"""
tools/dashboard/server.py
==========================

A local, dependency-free (Python standard library only) web dashboard for
live-monitoring and controlling the Orchestrator during manual live testing
against the Alpaca paper API.

WHAT THIS IS:
  - A tiny HTTP server (http.server, no Flask/FastAPI needed) that serves a
    single HTML page and a few JSON API endpoints.
  - It launches `python -m orchestrator.loop` (via `run_loop()`) as a
    SUBPROCESS, tails its stdout/stderr into a log file, and lets you view
    that log feed plus the live contents of orchestrator/state.json in a
    browser, with Start / Stop / Kill buttons.

WHAT THIS IS NOT:
  - It does NOT add any trading capability. It only starts/stops the
    existing orchestrator process and displays files that already exist.
    All paper-trading safety enforcement lives in orchestrator/loop.py
    exactly as before; this dashboard cannot bypass or affect it.

IMPORTANT WINDOWS CAVEAT (read before using "Stop (graceful)"):
  Windows does not reliably deliver SIGINT/SIGTERM to a background
  subprocess the way POSIX does. The "Stop (graceful)" button below sends
  CTRL_BREAK_EVENT to the child's process group as a best-effort attempt.
  This is USEFUL for casual stop/restart during testing, but it is NOT
  guaranteed to invoke the exact same code path as pressing Ctrl+C in a
  real terminal window running the orchestrator directly. If you need to
  rigorously verify Requirement 4's SIGINT-handler behavior specifically,
  run the orchestrator in its own terminal (`python -c "from
  orchestrator.loop import run_loop; run_loop()"`) and press Ctrl+C there.

  The "Kill" button, by contrast, is fully reliable on any OS — it calls
  proc.kill() (TerminateProcess on Windows), which is exactly the kind of
  ungraceful termination needed to simulate a real crash for the
  deferred-execution crash-recovery test.

HOW TO RUN:
  From the project root (C:\\Minions):
      python tools\\dashboard\\server.py
  Then open http://127.0.0.1:8765 in a browser.

  Stop the dashboard itself with Ctrl+C in the terminal you launched it from.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Resolve project root as two directories up from this file
# (tools/dashboard/server.py -> tools/dashboard -> tools -> PROJECT_ROOT).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

STATE_FILE_PATH = PROJECT_ROOT / "orchestrator" / "state.json"
LOG_FILE_PATH = Path(__file__).resolve().parent / "run.log"
INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"

HOST = "127.0.0.1"
PORT = 8765

MAX_LOG_LINES_RETURNED = 500

# ---------------------------------------------------------------------------
# Process management (single global subprocess handle)
# ---------------------------------------------------------------------------

_proc_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_log_file_handle = None  # kept open for the lifetime of the subprocess


def _is_running() -> bool:
    """True if we're tracking a subprocess and it hasn't exited."""
    with _proc_lock:
        return _proc is not None and _proc.poll() is None


def _start_orchestrator() -> dict:
    """
    Launch `run_loop()` as a subpress, redirecting its stdout/stderr to
    LOG_FILE_PATH (truncated on each start).

    On Windows, the child is created with CREATE_NEW_PROCESS_GROUP so that
    CTRL_BREAK_EVENT can later be targeted at it specifically (see
    _stop_orchestrator_graceful below) without also affecting this
    dashboard's own process.
    """
    global _proc, _log_file_handle

    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            return {"ok": False, "error": "Already running.", "pid": _proc.pid}

        # Truncate the log file for a clean run each time Start is pressed.
        _log_file_handle = open(LOG_FILE_PATH, "w", encoding="utf-8")

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        # We invoke run_loop() via -c rather than assuming a __main__.py
        # entry point exists in orchestrator/, since we don't want to
        # require adding one just for this dashboard to work.
        cmd = [
            sys.executable,
            "-c",
            "from orchestrator.loop import run_loop; run_loop()",
        ]

        _proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=_log_file_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

        return {"ok": True, "pid": _proc.pid}


def _stop_orchestrator_graceful() -> dict:
    """
    Best-effort graceful stop. See the Windows caveat in the module
    docstring — this is NOT guaranteed equivalent to a real Ctrl+C.
    """
    global _proc

    with _proc_lock:
        if _proc is None or _proc.poll() is not None:
            return {"ok": False, "error": "Not running."}

        try:
            if os.name == "nt":
                # CTRL_BREAK_EVENT requires the child to have been created
                # with CREATE_NEW_PROCESS_GROUP (done in _start_orchestrator).
                _proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                _proc.send_signal(signal.SIGTERM)
            return {"ok": True, "pid": _proc.pid, "method": "graceful"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _kill_orchestrator() -> dict:
    """
    Hard-kill the subprocess. Fully reliable on any OS. This is the
    correct button to use for the deferred-execution crash-recovery test —
    it does NOT give the process any chance to run shutdown/cleanup code,
    which is exactly what simulates a real crash.
    """
    global _proc

    with _proc_lock:
        if _proc is None or _proc.poll() is not None:
            return {"ok": False, "error": "Not running."}

        pid = _proc.pid
        _proc.kill()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return {"ok": True, "pid": pid, "method": "kill"}


def _status() -> dict:
    with _proc_lock:
        if _proc is None:
            return {"running": False, "pid": None, "returncode": None}
        returncode = _proc.poll()
        return {
            "running": returncode is None,
            "pid": _proc.pid,
            "returncode": returncode,
        }


# ---------------------------------------------------------------------------
# Helpers for serving state.json and the log tail
# ---------------------------------------------------------------------------

def _read_state_file() -> dict:
    if not STATE_FILE_PATH.exists():
        return {"_note": f"No state file yet at {STATE_FILE_PATH}"}
    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        return {"_error": f"Could not read/parse state file: {exc}"}


def _read_log_tail(max_lines: int = MAX_LOG_LINES_RETURNED) -> list:
    if not LOG_FILE_PATH.exists():
        return []
    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as exc:
        return [f"<error reading log file: {exc}>"]

    tail = lines[-max_lines:]
    parsed = []
    for line in tail:
        line = line.rstrip("\n")
        if not line:
            continue
        # Best-effort: if the line is JSON (structured log event), keep it
        # as a dict so the frontend can color-code by level. Otherwise pass
        # through as plain text.
        try:
            parsed.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            parsed.append({"raw": line})
    return parsed


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default request logging to keep the dashboard's own
        # terminal clean; comment this out if you want raw HTTP access logs.
        pass

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._serve_index()
        elif self.path == "/api/status":
            self._send_json(_status())
        elif self.path == "/api/state":
            self._send_json(_read_state_file())
        elif self.path.startswith("/api/logs"):
            self._send_json({"lines": _read_log_tail()})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/start":
            self._send_json(_start_orchestrator())
        elif self.path == "/api/stop":
            self._send_json(_stop_orchestrator_graceful())
        elif self.path == "/api/kill":
            self._send_json(_kill_orchestrator())
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_index(self):
        if not INDEX_HTML_PATH.exists():
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"index.html not found next to server.py")
            return
        body = INDEX_HTML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    LOG_FILE_PATH.touch(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Orchestrator dashboard running at http://{HOST}:{PORT}")
    print(f"Project root resolved as: {PROJECT_ROOT}")
    print(f"Watching state file: {STATE_FILE_PATH}")
    print("Press Ctrl+C here to stop the DASHBOARD (not the orchestrator).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()