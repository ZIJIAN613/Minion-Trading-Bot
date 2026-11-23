"""
log_system/file_handler.py — SafeFileHandler and (in task 2.2) initialize_file_logging().

This file currently contains SafeFileHandler (task 2.1). initialize_file_logging()
is added in task 2.2 (Wave 2); until then, log_system/__init__.py guards its import.

SafeFileHandler is a logging.FileHandler subclass whose one job is: a failing disk
write must NEVER crash or block the trading pipeline. On a write failure it:
  1. Emits a single WARNING to stderr (first failure only — no log spam), and
  2. If stderr ALSO fails (double failure), fires a fire-and-forget alert and
     then gives up silently.

Crucially, the stderr warning is written by RAW sys.stderr.write() — NOT through
the logging framework. See _emit_stderr_warning() for why this matters (the design
calls this out: logging.Handler.emit() swallows write errors internally, which
would make the double-failure branch unreachable dead code).

Requirements: 1.3, 1.4, 7.1, 7.3
"""

import json                                  # stdlib — build the stderr JSON line
import logging                               # stdlib — FileHandler base, LogRecord
import sys                                    # stdlib — sys.stderr
from datetime import datetime, timezone      # stdlib — UTC timestamp for the warning

from log_system.formatter import _JsonFormatter  # shared formatter for the file handler


class SafeFileHandler(logging.FileHandler):
    """
    A FileHandler that never lets a write failure escape.

    Internal state (see design "SafeFileHandler Internal State" table):
        _file_healthy    — True while the file is writable. Flipped to False on
                           the FIRST failure; gates the single stderr warning so
                           repeated failures don't spam stderr. Reset to True on
                           a later successful write (recovery case).
        _alert_dispatched — True once the double-failure alert has been sent,
                           so at most ONE alert is emitted per run.
    """

    def __init__(self, *args, **kwargs) -> None:
        """
        Construct the handler and initialise the two health-tracking flags.

        We pass *args/**kwargs straight through to logging.FileHandler so this
        subclass accepts exactly the same constructor arguments as its base
        (filename, mode, encoding, delay, ...). This is the Python analogue of
        a C++ constructor that forwards to the base-class constructor.
        """
        super().__init__(*args, **kwargs)

        # Start optimistic: assume the file is writable until proven otherwise.
        self._file_healthy = True

        # No alert sent yet this run.
        self._alert_dispatched = False

    def emit(self, record: logging.LogRecord) -> None:
        """
        Write one record to the file, converting any OSError into a safe,
        non-propagating warning path.

        Behaviour (matches the design's Error Handling section exactly):
          - Success                       -> write, mark file healthy, return.
          - First OSError                 -> mark unhealthy, emit ONE stderr warning.
          - First OSError + stderr broken -> double-failure alert dispatch.
          - Subsequent OSErrors           -> silently suppressed (no spam).
        In all cases the caller never sees an exception.

        IMPORTANT — why we do the write ourselves instead of calling super().emit():
            A Hypothesis/integration check proved that logging.StreamHandler.emit()
            (the grandparent of FileHandler) wraps its stream write in its OWN
            try/except that routes failures to self.handleError() and prints a
            "--- Logging error ---" block to stderr, WITHOUT re-raising. If we
            called super().emit(), our `except OSError` below would be dead code:
            the OSError would be swallowed by the base class and our
            single-warning / double-failure logic would never run.

            So we replicate the minimal write StreamHandler.emit() performs —
            format the record, ensure the stream is open, write the line, flush —
            but WITHOUT the exception-swallowing wrapper. That way a real write
            failure surfaces as an OSError our handler can act on.
        """
        try:
            # self.stream is the open file object. FileHandler opens it lazily;
            # if delay=True was used and the stream is not open yet, _open() it.
            # (In our normal usage the stream is already open.)
            if self.stream is None:
                self.stream = self._open()

            # format() runs our _JsonFormatter -> the single-line JSON string.
            msg = self.format(record)

            # Write the line plus the handler's terminator (default "\n").
            # These two calls are where a disk-full / permission OSError shows up.
            self.stream.write(msg + self.terminator)
            self.flush()

            # Reached only if the write succeeded — (re)mark the file healthy so
            # that if the file recovers after a transient failure, a future
            # failure will again emit exactly one warning.
            self._file_healthy = True
        except OSError as exc:
            # OSError covers disk full, permission denied, file moved/unlinked,
            # etc. We ONLY act on the first failure in a healthy->unhealthy
            # transition; further failures are silently swallowed.
            if self._file_healthy:
                self._file_healthy = False
                try:
                    # Attempt the single stderr warning. This deliberately uses
                    # a raw write (see _emit_stderr_warning) so that a genuinely
                    # broken stderr raises here and we can detect double failure.
                    self._emit_stderr_warning(f"Log file write failed: {exc}")
                except Exception:
                    # stderr ALSO failed — we've lost both destinations. Try the
                    # last-resort alert. We catch broad Exception (not just
                    # OSError) because any failure of the stderr write means we
                    # must escalate.
                    self._dispatch_double_failure_alert(record)
            # If _file_healthy was already False, do nothing: suppress silently.

    def _emit_stderr_warning(self, message: str) -> None:
        """
        Write a WARNING-level JSON line DIRECTLY to sys.stderr, bypassing logging.

        WHY bypass the logging framework (this is the subtle part):
            logging.Handler.emit() internally wraps its write in a try/except
            that routes failures to self.handleError() and SUPPRESSES them. If we
            used a StreamHandler/Logger here, a genuinely broken stderr (closed
            file descriptor, broken pipe) would be swallowed silently and never
            raise — which would make the double-failure branch in emit()
            unreachable dead code. By writing to sys.stderr directly and calling
            flush(), a broken stderr surfaces as an OSError we can catch.

        The output conforms to the same JSON schema as every other log line
        (event/level/timestamp/module) so the dashboard's "every stderr line is
        JSON" expectation still holds.

        Args:
            message: Human-readable detail included as the "message" field.

        Raises:
            OSError (or subclass): if stderr is broken. This is INTENTIONAL — the
            caller (emit) uses the exception to detect the double-failure case.
        """
        # Build the timestamp the same way the formatter does: UTC, 'Z' suffix.
        # Note we use strftime with a whole-second format here; this line is a
        # diagnostic and does not need sub-second precision.
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # json.dumps builds a valid single-line JSON object and handles escaping
        # of any special characters in `message` (e.g. Windows backslashes).
        entry = json.dumps({
            "event": "file_write_failed",
            "level": "WARNING",
            "timestamp": timestamp,
            "module": "log_system",
            "message": message,
        })

        # Raw write + flush. flush() forces the bytes out now; if the underlying
        # file descriptor is closed/broken, THIS is where OSError is raised.
        sys.stderr.write(entry + "\n")
        sys.stderr.flush()

    def _dispatch_double_failure_alert(self, record: logging.LogRecord) -> None:
        """
        Last-resort alert when BOTH the file write and the stderr warning failed.

        Guarantees:
          - At most one alert per run (guarded by self._alert_dispatched).
          - Never raises — the entire dispatch is wrapped in a bare except.
          - Does not re-enter the logging system (uses _suppress_logging=True on
            send_alert so the alerting module skips its own internal logging).

        Args:
            record: The LogRecord whose write failed (used to include the event
                    name in the alert context, best-effort).
        """
        # Only ever dispatch once — subsequent double failures are ignored.
        if self._alert_dispatched:
            return
        self._alert_dispatched = True

        try:
            # Import lazily and locally: importing alerting at module top level
            # could create an import cycle (alerting itself uses the logger), and
            # we only need it on this rare failure path.
            from alerting.sender import send_alert

            # _suppress_logging=True prevents send_alert from making its own
            # logging calls, which would re-enter THIS handler and risk infinite
            # recursion. NOTE: the _suppress_logging parameter is ADDED in task
            # 5.1. Until then this kwarg raises TypeError — which is harmless
            # here because the surrounding `except Exception: pass` swallows it
            # (we have already exhausted our escalation options at this point).
            send_alert(
                "logging_both_destinations_failed",
                {"event": getattr(record, "event", "unknown")},
                _suppress_logging=True,
            )
        except Exception:
            # Both destinations are down and alerting failed too — there is
            # nothing more we can safely do. Swallow and continue so the trading
            # loop is never interrupted by logging.
            pass


# ===========================================================================
# entry_point validation (task 2.3) + initialize_file_logging() (task 2.2)
# ===========================================================================

import os                                    # stdlib — path building, mkdir
from datetime import datetime as _dt         # aliased to avoid clashing with the
                                             # datetime imported at module top


# The characters we allow in an entry_point. Restricting to ASCII letters,
# digits, and underscore guarantees the value can never contain a path
# separator ("/" or "\"), a drive prefix ("C:"), "..", or any other shell/FS
# metacharacter — so it cannot escape the logs/ directory (path traversal).
_ALLOWED_ENTRY_POINT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_"
)


def _validate_entry_point(entry_point: str) -> None:
    """
    Validate that `entry_point` is safe to embed in a log file path.

    We follow the project's existing input-validation convention (as in
    risk_engine/config_loader.py): raise a ValueError with a clear message on
    bad input, rather than silently sanitising. A bad entry_point is a caller
    bug (a hard-coded literal like "orchestrator" or a script name), so failing
    loudly at startup is preferable to quietly writing to an unexpected path.

    # NOTE (why this matters): entry_point becomes part of a filesystem path
    # (logs/<timestamp>_<entry_point>.log). If a caller passed something like
    # "../secrets" or "C:\\Windows\\x", an unvalidated value could redirect the
    # log file OUTSIDE the logs/ directory — a path-traversal risk. Restricting
    # to an alphanumeric+underscore allow-list makes that structurally
    # impossible. It also preserves the filename invariant Property 5 relies on.

    Args:
        entry_point: The run identifier (e.g. "orchestrator", "fetch_aapl").

    Raises:
        ValueError: if entry_point is not a non-empty str of allowed characters.
    """
    # Must be a string. (bool is not a str, so no bool-is-int trap here, but we
    # still reject non-str types explicitly for a clear error.)
    if not isinstance(entry_point, str):
        raise ValueError(
            f"entry_point must be a string, got {type(entry_point).__name__}."
        )

    # Must be non-empty — an empty name would produce "logs/<ts>_.log".
    if entry_point == "":
        raise ValueError("entry_point must be a non-empty string.")

    # Every character must be in the allow-list. We scan explicitly (rather than
    # a terse regex) so the intent is obvious to a reader new to Python.
    # Building the set of offending characters gives a helpful error message.
    bad_chars = sorted({ch for ch in entry_point if ch not in _ALLOWED_ENTRY_POINT_CHARS})
    if bad_chars:
        raise ValueError(
            f"Invalid entry_point {entry_point!r}: only letters, digits, and "
            f"underscore are allowed. Offending character(s): {bad_chars}. "
            f"(This restriction prevents the log file from escaping the logs/ "
            f"directory via path separators or '..'.)"
        )


def initialize_file_logging(entry_point: str, log_dir: str = "logs") -> bool:
    """
    Create the per-run log file and attach a SafeFileHandler to all loggers.

    Steps:
      1. Validate entry_point (task 2.3) — raises ValueError on unsafe input.
      2. Create the log_dir (default "logs/") if it does not exist.
      3. Generate the filename "<YYYYMMDD>_<HHMMSS>_<entry_point>.log" using
         the CURRENT UTC time.
      4. Open a SafeFileHandler on that path and attach the shared _JsonFormatter.
      5. Publish the handler via logger.set_file_handler() so FUTURE get_logger()
         calls attach it immediately.
      6. RETROACTIVELY attach the handler to every already-created logger that
         carries the _log_system_managed sentinel (loggers built at import time,
         before this function ran).

    On any OSError during directory creation or file open (permissions, disk
    full), we do NOT raise: we emit a WARNING to stderr and return False so the
    run continues with console-only logging (Requirements 2.5, 7.2). Note that
    an invalid entry_point DOES raise (step 1) — that is a caller bug, not an
    environmental failure.

    Args:
        entry_point: Run identifier — "orchestrator" for a full run, or a
                     standalone script's name (e.g. "fetch_aapl").
        log_dir:     Directory for log files (default "logs" at the project root).

    Returns:
        True  — file logging initialised successfully.
        False — file logging unavailable; console-only fallback in effect.

    Raises:
        ValueError: if entry_point contains unsafe characters (see
                    _validate_entry_point). This is intentional and separate
                    from the graceful OSError fallback.
    """
    # Step 1: validate BEFORE touching the filesystem.
    _validate_entry_point(entry_point)

    # Import the logger-state helpers lazily to avoid any import-order concerns
    # (logger.py imports formatter.py; this module also imports formatter.py).
    from log_system.logger import set_file_handler, _MANAGED_ATTR

    try:
        # Step 2: create the log directory if needed. os.makedirs with
        # exist_ok=True is the idiomatic "mkdir -p" — it does not error if the
        # directory already exists. This is cross-platform (works on Windows).
        os.makedirs(log_dir, exist_ok=True)

        # Step 3: build the filename from the current UTC time.
        # strftime with these codes yields e.g. "20250115_143052".
        now = _dt.now(timezone.utc)
        stamp = now.strftime("%Y%m%d_%H%M%S")
        filename = f"{stamp}_{entry_point}.log"

        # os.path.join uses the OS-correct separator (backslash on Windows).
        filepath = os.path.join(log_dir, filename)

        # Step 4: open the SafeFileHandler and attach the shared JSON formatter.
        handler = SafeFileHandler(filepath, encoding="utf-8")
        handler.setFormatter(_JsonFormatter())
    except OSError as exc:
        # Environmental failure (permissions/disk). Warn to stderr and fall back
        # to console-only. We write a plain JSON line directly rather than via
        # the logging framework, since file logging is exactly what just failed.
        _warn_init_failure(f"File logging unavailable: {exc}")
        return False

    # Step 5: publish the handler for future get_logger() calls.
    set_file_handler(handler)

    # Step 6: retroactively attach to loggers already created at import time.
    # logging.Logger.manager.loggerDict maps logger name -> Logger (or a
    # PlaceHolder for intermediate names). We attach only to real Logger objects
    # that carry our sentinel, and guard against double-adding the handler.
    for existing in list(logging.Logger.manager.loggerDict.values()):
        if not isinstance(existing, logging.Logger):
            continue  # skip PlaceHolder entries in the hierarchy
        if getattr(existing, _MANAGED_ATTR, False) and handler not in existing.handlers:
            existing.addHandler(handler)

    return True


def _warn_init_failure(message: str) -> None:
    """
    Write a WARNING to stderr as a JSON line, bypassing the logging framework.

    Used when initialize_file_logging() cannot create the log file. We avoid the
    logging system here for the same reason as SafeFileHandler._emit_stderr_warning:
    the file path is exactly what failed, and we want a dependency-free, direct
    diagnostic. Unlike that method, we do NOT want this to raise, so we swallow a
    broken-stderr error (there is nothing more we can do at init time).
    """
    entry = json.dumps({
        "event": "file_logging_init_failed",
        "level": "WARNING",
        "timestamp": _dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "module": "log_system",
        "message": message,
    })
    try:
        sys.stderr.write(entry + "\n")
        sys.stderr.flush()
    except Exception:
        pass  # stderr also unavailable — nothing more we can safely do.
