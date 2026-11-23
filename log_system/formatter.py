"""
log_system/formatter.py — Structured JSON log formatter (shared across all modules).

This class was moved here from data_ingestion/logger.py so that every module in
the Minions bot (Data Ingestion, Strategy, Risk Engine, Execution, Orchestrator,
Alerting) shares ONE formatter. File output and console (stderr) output therefore
stay byte-for-byte identical — there is no second formatter to drift out of sync.

Every log record is serialised to a single-line JSON object, e.g.:
    {"event": "cycle_start", "level": "INFO", "timestamp": "2025-01-01T00:00:00Z", "module": "orchestrator", ...}

Difference from the original data_ingestion version:
    The original emitted only event/level/timestamp. This version ALSO emits a
    mandatory "module" field, taken from the LogRecord's `name` attribute (i.e.
    the logger name passed to get_logger(), such as "risk_engine"). See the
    _MODULE note below for the Python-specific subtlety this avoids.

Security note:
    Credential values (APCA_API_KEY_ID, APCA_API_SECRET_KEY) MUST NEVER be
    passed as fields to log_structured. Only credential variable *names* may
    appear (e.g., in error messages). This is enforced by convention — callers
    are responsible for never passing credential values as keyword arguments.

Requirements: 1.2, 5.1, 5.2, 5.4, 8.1, 8.2
"""

import json                                  # stdlib — JSON serialisation
import logging                               # stdlib — LogRecord type + Formatter base
from datetime import datetime, timezone      # stdlib — UTC-aware timestamps


class _JsonFormatter(logging.Formatter):
    """
    A custom logging.Formatter subclass (Python's equivalent of a derived class)
    that serialises each LogRecord to a single-line JSON string.

    The standard logging.Formatter produces human-readable text like:
        INFO:root:some message
    This formatter instead produces:
        {"event": "some_event", "level": "INFO", "timestamp": "...Z", "module": "...", ...}

    The "event" and any extra fields are injected by log_structured() via the
    LogRecord's __dict__, which is the standard Python mechanism for passing
    structured data through the logging pipeline.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Convert a LogRecord into a JSON string.

        Python's logging module populates `record` with standard attributes
        (levelname, created, name, msg, etc.) and any key-value pairs passed via
        the extra={} argument to logger.log() / logger.info() / etc.

        We build a dict with our required fields, then add any extra fields
        that were injected by log_structured().
        """

        # Build the UTC timestamp in ISO 8601 format with a 'Z' suffix.
        # datetime.fromtimestamp(record.created, tz=timezone.utc) converts the
        # Unix epoch float (record.created) to a timezone-aware datetime object.
        utc_dt = datetime.fromtimestamp(record.created, tz=timezone.utc)

        # .isoformat() produces "2024-01-01T00:00:00+00:00"; we replace the
        # "+00:00" suffix with "Z" to match the ISO 8601 UTC shorthand.
        # Requirement 8.1/8.2: the string form MUST end with 'Z'.
        timestamp_str = utc_dt.isoformat().replace("+00:00", "Z")

        # ------------------------------------------------------------------
        # The "module" field — a Python-specific subtlety worth calling out.
        # ------------------------------------------------------------------
        # A LogRecord already HAS a built-in attribute called `record.module`,
        # but it is NOT what we want: Python's logging sets record.module to the
        # source *filename* (without extension) where the log call happened —
        # e.g. "loop" for orchestrator/loop.py. That is an implementation detail,
        # not the logical module name.
        #
        # What we want for the JSON "module" field is the LOGGER NAME (the string
        # passed to get_logger(), e.g. "risk_engine"), which lives in
        # `record.name`. So we deliberately read record.name here, and we keep
        # the built-in record.module in the skip-set below so it never leaks in.
        module_name = record.name

        # Start the output dict with the four mandatory fields.
        # dict() with keyword arguments is Python's equivalent of initialiser
        # list syntax — keys become string keys in the mapping.
        output: dict = {
            "event":     record.__dict__.get("event", record.getMessage()),
            "level":     record.levelname,
            "timestamp": timestamp_str,
            "module":    module_name,
        }

        # ------------------------------------------------------------------
        # Collect caller-supplied extra fields. There are TWO shapes to support:
        # ------------------------------------------------------------------
        # (a) NEW shape (from log_system.log_structured): all caller fields are
        #     bundled into a single record attribute "_fields" (a dict). This
        #     avoids collisions with built-in LogRecord attribute names — see
        #     the detailed note in logger.log_structured(). We read them first
        #     so they are present, then let any legacy top-level extras (b)
        #     supplement them.
        #
        # (b) LEGACY shape (from existing callers that do
        #     logger.warning(..., extra={"event": ..., "foo": ...}) directly,
        #     such as orchestrator/loop.py and data_ingestion/__init__.py):
        #     the extra fields live as top-level record attributes. We copy any
        #     that are not Python internals or our reserved keys.
        #
        # Supporting both shapes is what lets the backward-compatibility shim
        # (task 4.3) work without touching existing call sites.

        # (a) New shape: pull the bundled dict, if present.
        # getattr(obj, name, default) is Python's "obj.name if it exists else
        # default" — like a C++ ternary guarded by has-attr.
        bundled = getattr(record, "_fields", None)
        if isinstance(bundled, dict):
            for key, value in bundled.items():
                output[key] = value

        # (b) Legacy shape: copy top-level extras that aren't internals/reserved.
        _INTERNAL_ATTRS = frozenset({
            # Standard LogRecord attributes — Python logging internals, not
            # meaningful to downstream log consumers.
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
            # Our own reserved output keys — already set above.
            "event", "level", "timestamp",
            # The bundle attribute itself — already unpacked in (a).
            "_fields",
        })
        for key, value in record.__dict__.items():
            if key not in _INTERNAL_ATTRS:
                output[key] = value

        # json.dumps() serialises the dict to a compact single-line JSON string.
        # ensure_ascii=False preserves any Unicode characters in field values.
        # default=str converts any non-serialisable objects (e.g. datetime) to
        # their string representation rather than raising a TypeError.
        return json.dumps(output, ensure_ascii=False, default=str)
