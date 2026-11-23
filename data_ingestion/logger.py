"""
logger.py — Structured JSON logging for the data_ingestion module.

All other components in data_ingestion/ obtain their logger via get_logger().
Every log record is emitted as a single-line JSON object to stderr, e.g.:
    {"event": "module_initialised", "level": "INFO", "timestamp": "2024-01-01T00:00:00Z", ...}

Security note:
    Credential values (APCA_API_KEY_ID, APCA_API_SECRET_KEY) MUST NEVER be
    passed as fields to log_structured. Only credential variable *names* may
    appear (e.g., in error messages). This is enforced by convention — callers
    are responsible for never passing credential values as keyword arguments.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 1.3
"""

import json
import logging
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    """
    A custom logging.Formatter subclass (Python's equivalent of a derived class)
    that serialises each LogRecord to a single-line JSON string.

    The standard logging.Formatter produces human-readable text like:
        INFO:root:some message
    This formatter instead produces:
        {"event": "some_event", "level": "INFO", "timestamp": "...Z", ...}

    The "event" and any extra fields are injected by log_structured() via the
    LogRecord's __dict__, which is the standard Python mechanism for passing
    structured data through the logging pipeline.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Convert a LogRecord into a JSON string.

        Python's logging module populates record with standard attributes
        (levelname, created, msg, etc.) and any key-value pairs passed via
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
        timestamp_str = utc_dt.isoformat().replace("+00:00", "Z")

        # Start the output dict with the three mandatory fields.
        # dict() with keyword arguments is Python's equivalent of initialiser
        # list syntax — keys become string keys in the mapping.
        output: dict = {
            "event":     record.__dict__.get("event", record.getMessage()),
            "level":     record.levelname,
            "timestamp": timestamp_str,
        }

        # Collect any additional fields injected via extra={} in log_structured().
        # We skip internal LogRecord attributes and our three reserved keys to
        # avoid cluttering the JSON output with Python internals.
        _INTERNAL_ATTRS = frozenset({
            # Standard LogRecord attributes — these come from Python's logging
            # internals and are not meaningful to downstream log consumers.
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
            # Our own reserved output keys — already included above.
            "event", "level", "timestamp",
        })

        for key, value in record.__dict__.items():
            if key not in _INTERNAL_ATTRS:
                output[key] = value

        # json.dumps() serialises the dict to a compact single-line JSON string.
        # ensure_ascii=False preserves any Unicode characters in field values.
        # default=str converts any non-serialisable objects (e.g. datetime) to
        # their string representation rather than raising a TypeError.
        return json.dumps(output, ensure_ascii=False, default=str)


def get_logger(name: str = "data_ingestion") -> logging.Logger:
    """
    Return a Logger configured to emit structured JSON records.

    If a logger with this name already has handlers attached (e.g., because
    get_logger() was called more than once with the same name), we skip adding
    another handler to avoid duplicate log lines.

    Args:
        name: The logger name. Using the module hierarchy (e.g.,
              "data_ingestion.rest_client") is idiomatic Python — it mirrors
              how C++ namespaces are nested.

    Returns:
        A logging.Logger instance ready to use.
    """

    logger = logging.getLogger(name)

    # Only configure the logger once. In Python, loggers are global singletons
    # keyed by name, so calling getLogger() with the same name always returns
    # the same object. We check logger.handlers directly (not hasHandlers())
    # because hasHandlers() also searches parent loggers in the hierarchy —
    # it would return True if the root logger has a handler, which would
    # incorrectly skip configuring this logger. Checking the local list is
    # the C++ equivalent of checking a member variable rather than a virtual
    # method that walks the class hierarchy.
    if not logger.handlers:
        # Emit to stderr (sys.stderr) so log output does not mix with stdout
        # data payloads. This mirrors C++'s std::cerr convention.
        handler = logging.StreamHandler(sys.stderr)

        # Attach our custom JSON formatter to the handler.
        handler.setFormatter(_JsonFormatter())

        logger.addHandler(handler)

        # Set the minimum level to DEBUG so all log levels pass through.
        # Individual call sites control their level via log_structured().
        logger.setLevel(logging.DEBUG)

    return logger


def log_structured(
    logger: logging.Logger,
    level: int,
    event: str,
    # **fields collects all remaining keyword arguments into a dict.
    # This is Python's "keyword variadic arguments" — equivalent to a
    # variadic template pack in C++ (template<typename... Args>), but
    # captured at runtime as a plain dict[str, Any].
    # Example: log_structured(log, logging.INFO, "foo", bar=1, baz="x")
    # → fields == {"bar": 1, "baz": "x"}
    #
    # SECURITY: Never pass credential VALUES as fields. Only pass non-sensitive
    # data (symbol names, timestamps, counts, etc.). The credential variables
    # APCA_API_KEY_ID and APCA_API_SECRET_KEY must never appear as values here.
    **fields,
) -> None:
    """
    Thin wrapper that emits a structured JSON log record.

    Packs the event name and all extra fields into logging's 'extra' dict,
    which the _JsonFormatter reads out when building the JSON output.

    Args:
        logger: The Logger instance to write to (obtained via get_logger()).
        level:  A logging level constant: logging.DEBUG, logging.INFO,
                logging.WARNING, logging.ERROR, or logging.CRITICAL.
        event:  The string value for the "event" JSON field, e.g.
                "module_initialised" or "historical_fetch_complete".
        **fields: Additional key-value pairs merged into the JSON object.
                  MUST NOT include credential values (see security note above).

    Example:
        log_structured(logger, logging.INFO, "module_initialised",
                       symbols=["AAPL", "SPY"], timeframe="1d")
        # emits: {"event": "module_initialised", "level": "INFO",
        #         "timestamp": "2024-01-01T00:00:00Z",
        #         "symbols": ["AAPL", "SPY"], "timeframe": "1d"}
    """

    # Merge the event name with any extra fields into a single dict.
    # The dict literal {"event": event, **fields} unpacks the fields dict
    # and adds each key-value pair into the new dict — this is Python's
    # "dict unpacking" idiom (** in a dict literal). It is the equivalent
    # of calling std::map::merge() or inserting each pair individually.
    extra: dict = {"event": event, **fields}

    # logger.log() is the general form that accepts a level integer.
    # We pass an empty string as the message because the formatter reads
    # everything it needs from the 'extra' dict via the LogRecord's __dict__.
    logger.log(level, "", extra=extra)
