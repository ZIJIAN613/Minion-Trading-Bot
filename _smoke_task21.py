import io, json, logging, sys, tempfile, os
from log_system.file_handler import SafeFileHandler
from log_system.formatter import _JsonFormatter

tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "test.log")
h = SafeFileHandler(path, encoding="utf-8")
h.setFormatter(_JsonFormatter())

rec = logging.LogRecord("mod", logging.INFO, "somefile.py", 1, "", None, None)
rec.event = "e1"

# 1) normal write
h.emit(rec)
h.flush()
with open(path, encoding="utf-8") as f:
    line = f.readline()
obj = json.loads(line)
assert obj["event"] == "e1", obj
assert h._file_healthy is True
print("normal write OK ->", obj)

# 2) inject a stream whose write() raises OSError; capture stderr
class BadStream:
    def write(self, *a):
        raise OSError("disk full")
    def flush(self):
        pass

h.stream = BadStream()
cap = io.StringIO()
old = sys.stderr
sys.stderr = cap
try:
    h.emit(rec)  # first failure -> one stderr warning
    h.emit(rec)  # second failure -> silent (no spam)
finally:
    sys.stderr = old
warn_lines = [l for l in cap.getvalue().splitlines() if l.strip()]
assert len(warn_lines) == 1, warn_lines
w = json.loads(warn_lines[0])
assert w["event"] == "file_write_failed" and w["level"] == "WARNING", w
assert set(["event", "level", "timestamp", "module"]).issubset(w.keys()), w
assert h._file_healthy is False
print("single-warning + no-spam OK ->", w)

# 3) recovery: restore a good stream, emit -> _file_healthy resets True
h.stream = open(path, "a", encoding="utf-8")
h.emit(rec)
h.flush()
assert h._file_healthy is True
print("recovery reset OK")

# 4) double-failure: both file write AND stderr broken -> alert dispatch guarded
class BadStderr:
    def write(self, *a):
        raise OSError("stderr broken")
    def flush(self):
        raise OSError("stderr broken")

h.stream = BadStream()
h._file_healthy = True  # reset to force a first-failure transition
old = sys.stderr
sys.stderr = BadStderr()
try:
    # Should not raise; _dispatch_double_failure_alert runs (send_alert lacks
    # config here so it no-ops, and any error is swallowed).
    h.emit(rec)
finally:
    sys.stderr = old
assert h._alert_dispatched is True
print("double-failure guard OK (_alert_dispatched=True, no exception propagated)")

print("ALL SMOKE CHECKS PASSED")
