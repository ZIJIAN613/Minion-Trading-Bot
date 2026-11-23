"""
log_system/ — Centralized structured logging for the Minions paper-trading bot.

Public API:
    get_logger(name)                     -> logging.Logger
    log_structured(logger, level, event, **fields) -> None
    initialize_file_logging(entry_point) -> bool

Usage:
    from log_system import get_logger, log_structured, initialize_file_logging

    logger = get_logger("orchestrator")            # at import time is fine
    ...
    initialize_file_logging(entry_point="orchestrator")  # once, in the entry point
    log_structured(logger, logging.INFO, "cycle_start", cycle=1)

This package replaces the logging infrastructure that previously lived in
data_ingestion/logger.py. That file remains as a thin backward-compatibility
shim (see task 4.3) so existing imports keep working.
"""

# get_logger / log_structured live in logger.py and have no dependency on the
# file handler, so they import cleanly and eagerly here.
from log_system.logger import get_logger, log_structured

# initialize_file_logging lives in file_handler.py. We import it here so it is
# part of the public API. file_handler.py is created in Wave 1; until then this
# import would fail, so we guard it.
#
# Python note: `__all__` documents the public names of this package (what
# `from log_system import *` would export). We list initialize_file_logging in
# __all__ regardless, because it IS part of the intended public surface even if
# the implementing module has not been added yet.
try:
    from log_system.file_handler import initialize_file_logging
except ImportError:
    # file_handler.py not present yet (Wave 0 only). Provide a clear placeholder
    # so that any accidental early use fails with an actionable message rather
    # than a confusing AttributeError. This branch disappears once Wave 1 adds
    # file_handler.py.
    def initialize_file_logging(*args, **kwargs):  # type: ignore[misc]
        raise NotImplementedError(
            "initialize_file_logging() is implemented in log_system/file_handler.py "
            "(added in Wave 1 of the file-logging spec). It is not available yet."
        )


__all__ = [
    "get_logger",
    "log_structured",
    "initialize_file_logging",
]
