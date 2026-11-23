"""
tests/test_logger.py — Unit tests for data_ingestion/logger.py

Covers:
  - get_logger() returns a configured Logger with a StreamHandler
  - Calling get_logger() twice with the same name does not duplicate handlers
  - log_structured() emits valid single-line JSON to stderr
  - The emitted JSON contains "event", "level", and "timestamp" fields
  - The timestamp field ends with "Z" (UTC ISO 8601)
  - Extra **fields are merged into the JSON output
  - The "level" field matches the Python logging level name
"""

import io
import json
import logging

import pytest

# Import the module under test.
# In Python, relative imports within a package use leading dots (from . import ...),
# but test files outside the package use the full module path.
from data_ingestion.logger import get_logger, log_structured


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_logs(name: str, level: int, event: str, **fields) -> dict:
    """
    Helper: create a fresh logger wired to an in-memory StringIO buffer,
    call log_structured(), and return the parsed JSON dict.

    We create a brand-new logger (not via get_logger so we can capture output)
    and attach a StreamHandler pointing at a StringIO object — Python's
    equivalent of redirecting stderr to a string buffer.
    """
    # StringIO is an in-memory text stream — lets us capture log output without
    # touching the real stderr.
    buffer = io.StringIO()

    # Build a new, isolated logger for this test so tests don't interfere with
    # each other through the global logger registry.
    logger = logging.getLogger(f"test.{name}.{id(buffer)}")
    logger.setLevel(logging.DEBUG)
    # Prevent log records from propagating to the root logger (and appearing in
    # the real console). propagate=False is the standard way to isolate a logger.
    logger.propagate = False

    # Import the private formatter class to attach it to our test handler.
    from data_ingestion.logger import _JsonFormatter
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)

    log_structured(logger, level, event, **fields)

    raw = buffer.getvalue().strip()
    assert raw, "log_structured() emitted no output"
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tests for get_logger()
# ---------------------------------------------------------------------------

class TestGetLogger:
    def test_returns_logger_instance(self):
        """get_logger() must return a logging.Logger object."""
        logger = get_logger("test.returns_logger")
        assert isinstance(logger, logging.Logger)

    def test_has_stream_handler(self):
        """The returned logger must have exactly one StreamHandler attached."""
        logger = get_logger("test.has_handler")
        stream_handlers = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) == 1

    def test_no_duplicate_handlers_on_repeated_call(self):
        """Calling get_logger() with the same name twice must not add a second handler."""
        name = "test.no_duplicate"
        logger1 = get_logger(name)
        handler_count_before = len(logger1.handlers)

        logger2 = get_logger(name)
        handler_count_after = len(logger2.handlers)

        # Both calls must return the same object (Python's global logger registry).
        assert logger1 is logger2
        assert handler_count_after == handler_count_before

    def test_level_is_debug(self):
        """The logger's effective level must be DEBUG so all records pass through."""
        logger = get_logger("test.level_debug")
        assert logger.level == logging.DEBUG


# ---------------------------------------------------------------------------
# Tests for log_structured() / _JsonFormatter
# ---------------------------------------------------------------------------

class TestLogStructured:
    def test_output_is_valid_json(self):
        """log_structured() must emit a string that parses without error as JSON."""
        # If json.loads() raises, pytest reports the exception — test fails.
        result = _capture_logs("valid_json", logging.INFO, "test_event")
        assert isinstance(result, dict)

    def test_event_field_present(self):
        """The emitted JSON must have an 'event' key matching the argument."""
        result = _capture_logs("event_field", logging.INFO, "my_event")
        assert result["event"] == "my_event"

    def test_level_field_present(self):
        """The emitted JSON must have a 'level' key matching the logging level name."""
        result = _capture_logs("level_field", logging.INFO, "e")
        assert result["level"] == "INFO"

    def test_level_field_warning(self):
        """The 'level' field must reflect WARNING when that level is used."""
        result = _capture_logs("level_warning", logging.WARNING, "e")
        assert result["level"] == "WARNING"

    def test_level_field_debug(self):
        """The 'level' field must reflect DEBUG when that level is used."""
        result = _capture_logs("level_debug", logging.DEBUG, "e")
        assert result["level"] == "DEBUG"

    def test_level_field_error(self):
        """The 'level' field must reflect ERROR when that level is used."""
        result = _capture_logs("level_error", logging.ERROR, "e")
        assert result["level"] == "ERROR"

    def test_timestamp_field_present(self):
        """The emitted JSON must have a 'timestamp' key."""
        result = _capture_logs("timestamp_present", logging.INFO, "e")
        assert "timestamp" in result

    def test_timestamp_ends_with_z(self):
        """The timestamp must end with 'Z', indicating UTC."""
        result = _capture_logs("timestamp_z", logging.INFO, "e")
        assert result["timestamp"].endswith("Z"), (
            f"Timestamp '{result['timestamp']}' does not end with 'Z'"
        )

    def test_timestamp_is_iso8601_format(self):
        """The timestamp must be a valid ISO 8601 datetime string."""
        from datetime import datetime
        result = _capture_logs("timestamp_iso", logging.INFO, "e")
        ts = result["timestamp"]
        # Strip the trailing 'Z' and parse — fromisoformat() does not accept 'Z'
        # in Python < 3.11, so we replace it with '+00:00' for compatibility.
        # (Python 3.11+ accepts 'Z' directly, but we support 3.10 as well.)
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None, "Timestamp must be timezone-aware"

    def test_extra_fields_included(self):
        """Extra **fields must appear as top-level keys in the JSON output."""
        result = _capture_logs(
            "extra_fields",
            logging.INFO,
            "module_initialised",
            symbols=["AAPL", "SPY"],
            timeframe="1d",
        )
        assert result["symbols"] == ["AAPL", "SPY"]
        assert result["timeframe"] == "1d"

    def test_integer_extra_field(self):
        """Integer extra fields must be preserved as integers (not strings)."""
        result = _capture_logs("int_field", logging.DEBUG, "fetch_complete", bar_count=42)
        assert result["bar_count"] == 42
        assert isinstance(result["bar_count"], int)

    def test_no_python_internals_in_output(self):
        """Internal LogRecord attributes (lineno, funcName, etc.) must not appear."""
        result = _capture_logs("no_internals", logging.INFO, "e")
        internal_keys = {"lineno", "funcName", "pathname", "filename", "module",
                         "thread", "threadName", "process", "processName", "msecs"}
        leaked = internal_keys & result.keys()
        assert not leaked, f"Internal LogRecord keys leaked into JSON: {leaked}"

    def test_single_line_output(self):
        """Each log record must be a single line (no embedded newlines in the JSON)."""
        import io
        from data_ingestion.logger import _JsonFormatter

        buffer = io.StringIO()
        logger = logging.getLogger(f"test.single_line.{id(buffer)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.StreamHandler(buffer)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)

        log_structured(logger, logging.INFO, "first_event", x=1)
        log_structured(logger, logging.INFO, "second_event", x=2)

        lines = [l for l in buffer.getvalue().splitlines() if l.strip()]
        assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

    def test_multiple_extra_fields_all_present(self):
        """All keyword arguments must appear in the output when multiple are passed."""
        result = _capture_logs(
            "multi_extra",
            logging.WARNING,
            "websocket_reconnecting",
            symbols=["SPY"],
            attempt=3,
            backoff_seconds=4.0,
        )
        assert result["event"] == "websocket_reconnecting"
        assert result["symbols"] == ["SPY"]
        assert result["attempt"] == 3
        assert result["backoff_seconds"] == 4.0
