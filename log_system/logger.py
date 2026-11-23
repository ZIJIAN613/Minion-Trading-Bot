"""
log_system/logger.py — get_logger() and log_structured() for the shared logging system.

This is the entry point every module uses to obtain a logger:

    from log_system import get_logger, log_structured
    logger = get_logger("risk_engine")
    log_structured(logger, logging.INFO, "risk_evaluation", decision="APPROVED")

Design notes (see .kiro/specs/file-logging/design.md):
  - get_logger() attaches a stderr StreamHandler immediately, so logging works
    the instant a module is imported — even before file logging is initialised.
  - Loggers are tagged with a sentinel attribute `_log_system_managed = True`
    so that initialize_file_logging() (in file_handler.py) can find them later
    and RETROACTIVELY attach the per-run file handler. This is required because
    the existing codebase calls get_logger() at import time, which happens
    BEFORE any entry point calls initialize_file_logging().
  - The single source of truth for "has file logging been initialised, and if so
    what handler do new loggers get?" is the module-level `_file_handler` variable
    below. file_handler.py sets it via set_file_handler(); get_logger() reads it.

Requirements: 1.1, 1.2, 5.2, 5.3, 5.5
"""

import logging   # stdlib — Logger, StreamHandler, level constants
import sys        # stdlib — sys.stderr

from log_system.formatter import _JsonFormatter


# ---------------------------------------------------------------------------
# Sentinel attribute name
# ---------------------------------------------------------------------------
# We stamp every logger we create with an attribute of this name set to True.
# Using a module-level constant (rather than typing the raw string in several
# places) avoids typos and documents intent. This is the marker that
# initialize_file_logging() looks for when it walks the logger registry.
_MANAGED_ATTR = "_log_system_managed"


# ---------------------------------------------------------------------------
# Shared module-level state: the per-run file handler (if any)
# ---------------------------------------------------------------------------
# _file_handler is None until initialize_file_logging() succeeds, after which
# file_handler.py calls set_file_handler(handler) to publish the live handler
# here. get_logger() then attaches it to every NEW logger it creates.
#
# Python note: a bare module-level variable IS the module's shared/global state
# (roughly like a file-scope `static` variable in C++). Functions that REASSIGN
# it must declare `global _file_handler` first; functions that only READ it do
# not need the declaration.
_file_handler = None  # type: logging.Handler | None


def set_file_handler(handler) -> None:
    """
    Publish the per-run file handler so future get_logger() calls attach it.

    Called once by initialize_file_logging() (in file_handler.py) after the
    SafeFileHandler has been created successfully. Passing None is allowed and
    resets the state to "no file handler" (used by tests for isolation).

    Args:
        handler: A logging.Handler (the SafeFileHandler) or None.
    """
    # `global` tells Python we intend to REASSIGN the module-level name, not
    # create a new local variable that shadows it. Without this line, the
    # assignment below would silently create a function-local `_file_handler`.
    global _file_handler
    _file_handler = handler


def get_file_handler():
    """
    Return the currently-published file handler, or None if file logging has
    not been initialised. Read-only accessor used by file_handler.py and tests.
    """
    return _file_handler


def get_logger(name: str = "app") -> logging.Logger:
    """
    Return a Logger configured to emit structured JSON records.

    On the FIRST call for a given name, this attaches:
      - a stderr StreamHandler (always), and
      - the per-run file handler (only if file logging has already been
        initialised — otherwise the handler is attached later, retroactively,
        by initialize_file_logging()).

    On subsequent calls with the same name, no handlers are added again (Python
    loggers are global singletons keyed by name, so we must guard against
    duplicate handlers that would double-print every line).

    Args:
        name: The logger name, also used as the JSON "module" field. Using the
              module name (e.g. "risk_engine") is idiomatic — it mirrors how
              C++ namespaces are nested.

    Returns:
        A logging.Logger instance ready to use.
    """
    logger = logging.getLogger(name)

    # Only configure the logger once. We check logger.handlers directly (not
    # hasHandlers()) because hasHandlers() also searches PARENT loggers in the
    # hierarchy — it would return True if the root logger has a handler, which
    # would incorrectly skip configuring this logger. Checking the local list
    # is the C++ equivalent of checking a member variable rather than a virtual
    # method that walks the class hierarchy.
    if not logger.handlers:
        # --- stderr handler (always attached) ---
        # Emit to stderr (sys.stderr) so log output does not mix with stdout
        # data payloads. This mirrors C++'s std::cerr convention.
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(_JsonFormatter())
        logger.addHandler(stream_handler)

        # Set the minimum level to DEBUG so all log levels pass through by
        # default. The configured log_level (Requirement 4) is applied later
        # via a separate helper; here we simply avoid filtering anything out.
        logger.setLevel(logging.DEBUG)

        # Stamp the sentinel attribute so initialize_file_logging() can find
        # this logger later and retroactively attach the file handler.
        # setattr(obj, "name", value) is the dynamic form of `obj.name = value`
        # — we use it with the named constant to keep the marker consistent.
        setattr(logger, _MANAGED_ATTR, True)

        # --- file handler (attached only if file logging is already live) ---
        # If initialize_file_logging() has already run, _file_handler is set and
        # we attach it right now. If not, this logger will receive the file
        # handler retroactively once initialization occurs.
        if _file_handler is not None:
            logger.addHandler(_file_handler)

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
    # data (symbol names, timestamps, counts, etc.).
    **fields,
) -> None:
    """
    Emit a structured JSON log record.

    'event' is a MANDATORY positional parameter (Requirement 5.3): if a caller
    forgets it, Python raises TypeError at the call site during development,
    rather than silently producing a log entry with no event name at runtime.

    The 'module' field is NOT passed here — it is supplied automatically by the
    _JsonFormatter from the logger's name (see formatter.py). 'level' and
    'timestamp' are likewise supplied internally, so those three fields can
    never be absent from the output.

    Args:
        logger: The Logger instance to write to (obtained via get_logger()).
        level:  A logging level constant: logging.DEBUG, logging.INFO,
                logging.WARNING, logging.ERROR, or logging.CRITICAL.
        event:  The string value for the "event" JSON field, e.g.
                "risk_evaluation" or "order_filled".
        **fields: Additional key-value pairs merged into the JSON object.
                  MUST NOT include credential values (see security note above).

    Example:
        log_structured(logger, logging.INFO, "signal_generated",
                       symbol="AAPL", signal="BUY")
        # emits: {"event": "signal_generated", "level": "INFO",
        #         "timestamp": "2025-01-01T00:00:00Z", "module": "strategy",
        #         "symbol": "AAPL", "signal": "BUY"}
    """
    # We must be careful HOW we attach caller fields to the LogRecord.
    #
    # PYTHON PITFALL (found by a Hypothesis property test): Python's logging
    # rejects any `extra={}` key that collides with a built-in LogRecord
    # attribute — e.g. "filename", "lineno", "module", "message". Passing such a
    # key raises KeyError from deep inside logging.makeRecord(), which would
    # CRASH the caller. A logging call must never do that (Requirement 7), and
    # callers must be free to use any field name they like (Requirement 5.5).
    #
    # Fix: we do NOT spread the caller's fields across many LogRecord attributes.
    # Instead we attach TWO reserved, collision-free attributes:
    #     event   — the mandatory event name, and
    #     _fields — the entire caller dict, kept intact under one safe key.
    # The _JsonFormatter reads these back out. Because "event" and "_fields" are
    # not built-in LogRecord attributes, they never collide, so ANY user field
    # name (including "filename") round-trips safely.
    extra: dict = {"event": event, "_fields": dict(fields)}

    # logger.log() is the general form that accepts a level integer. We pass an
    # empty string as the message because the formatter reads everything it
    # needs from the 'extra' dict via the LogRecord's __dict__.
    logger.log(level, "", extra=extra)


# ---------------------------------------------------------------------------
# apply_log_level (task 4.2) — enforce the configured minimum threshold
# ---------------------------------------------------------------------------

# Map the five accepted level NAMES to Python's numeric logging constants.
# We build this once at import time. logging.getLevelName("INFO") also works,
# but an explicit dict is clearer and avoids surprises with custom levels.
_LEVEL_NAME_TO_NUM = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def apply_log_level(logger: logging.Logger, level_name: str) -> None:
    """
    Set `level_name` as the EXACT minimum threshold for the logger AND every
    handler attached to it (Requirement 4.4).

    Why set the handlers too:
        A logging record is emitted by a handler only if its level is >= BOTH
        the logger's effective level AND that handler's own level. Our handlers
        are normally created at NOTSET (0), meaning "defer to the logger", so
        setting the logger level alone would usually suffice. But Requirement
        4.4 states no handler may be STRICTER than the configured level, so we
        set each handler's level to the same value defensively. This guarantees
        the configured level is the single, exact threshold — a handler can
        neither over- nor under-filter relative to it.

    Args:
        logger:     A logger obtained from get_logger().
        level_name: One of "DEBUG"/"INFO"/"WARNING"/"ERROR"/"CRITICAL"
                    (case-insensitive). Unknown values fall back to INFO, matching
                    load_log_level()'s own defaulting behaviour.
    """
    # Normalise and resolve to a numeric level; default to INFO if unrecognised
    # (keeps behaviour consistent with config_loader.load_log_level()).
    numeric = _LEVEL_NAME_TO_NUM.get(level_name.strip().upper(), logging.INFO)

    # Set the logger's threshold.
    logger.setLevel(numeric)

    # Set every attached handler to the same threshold so none is stricter.
    for handler in logger.handlers:
        handler.setLevel(numeric)
