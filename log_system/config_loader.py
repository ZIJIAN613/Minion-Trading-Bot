"""
log_system/config_loader.py — Load and validate the `log_level` config field.

Each module's existing config.yaml (data_ingestion/config.yaml, risk_engine/
config.yaml, etc.) may include an OPTIONAL `log_level` field. This module reads
that field and returns a validated, normalised level string.

Design decisions (see .kiro/specs/file-logging/design.md, Requirement 4):
  - Accepted values: DEBUG, INFO, WARNING, ERROR, CRITICAL (case-INSENSITIVE).
  - Absent / null / unrecognised  -> default to "INFO" (never raises).
  - This loader intentionally does NOT raise on a bad value: logging config
    should degrade gracefully rather than crash the trading loop. This differs
    from risk_engine/config_loader.py, which DOES raise — because a bad risk
    limit is dangerous, whereas a bad log level is merely cosmetic.

Requirements: 4.1, 4.2, 4.3
"""

import sys                          # stdlib — sys.stderr for the fallback warning
from dataclasses import dataclass   # stdlib — @dataclass auto-generates __init__/__repr__/__eq__

import yaml                         # PyYAML (already a project dependency)


# ---------------------------------------------------------------------------
# The five valid log levels
# ---------------------------------------------------------------------------
# We store them uppercase because that is the canonical form we return.
# A frozenset gives O(1) membership testing and cannot be mutated by accident
# (like a `const std::unordered_set` in C++).
_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# The default level when the field is absent, null, or invalid (Requirement 4.3).
_DEFAULT_LEVEL = "INFO"


# ---------------------------------------------------------------------------
# LoggingConfig dataclass
# ---------------------------------------------------------------------------
# @dataclass is a DECORATOR that auto-generates __init__, __repr__, and __eq__
# from the annotated fields below — the same pattern used by
# risk_engine/config_loader.py. Analogous to a C++ struct where those member
# functions are written for you.
@dataclass
class LoggingConfig:
    """
    Validated configuration for the log_system module.

    Fields:
        log_level — One of "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
                    (always stored uppercase).
    """
    log_level: str


def load_log_level(config_path: str) -> str:
    """
    Read the `log_level` field from the YAML file at config_path.

    This function is deliberately forgiving — it NEVER raises. Any problem
    (missing file, unreadable YAML, missing/None/invalid `log_level`) results
    in a WARNING written to stderr and the default level "INFO" being returned.

    Algorithm:
      Step 1 — open + parse the YAML file. Any I/O or parse error -> default.
      Step 2 — if the parsed document is not a mapping (dict) -> default.
      Step 3 — read the `log_level` key. Absent or None -> default (silent;
               absence is explicitly allowed by Requirement 4.3, so no warning).
      Step 4 — the value must be a string. Non-string (or bool) -> warn + default.
      Step 5 — uppercase it and check membership in the valid set.
               Valid   -> return the uppercased value.
               Invalid -> warn + default.

    Args:
        config_path: Path to a module's config.yaml.

    Returns:
        A validated, UPPERCASE level string. Guaranteed to be one of
        _VALID_LEVELS. Falls back to "INFO" on any problem.
    """
    # ------------------------------------------------------------------
    # Step 1: Open and parse the YAML file.
    # ------------------------------------------------------------------
    # 'with open(...) as f:' is a CONTEXT MANAGER — Python's RAII equivalent.
    # The file is closed automatically when the block exits, even on exception.
    #
    # We wrap the whole read in try/except because this loader must never
    # crash the caller. OSError covers "file not found" / permission problems;
    # yaml.YAMLError covers malformed YAML.
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)  # safe_load forbids arbitrary object construction
    except (OSError, yaml.YAMLError) as exc:
        _warn(f"Could not read log_level from '{config_path}': {exc}. "
              f"Defaulting to {_DEFAULT_LEVEL}.")
        return _DEFAULT_LEVEL

    # ------------------------------------------------------------------
    # Step 2: The document must be a mapping (dict).
    # ------------------------------------------------------------------
    # yaml.safe_load returns None for an empty file, a list for a sequence, etc.
    # If it isn't a dict, there is no `log_level` key to read.
    if not isinstance(raw, dict):
        # An empty or non-mapping config is a normal, silent fallback: there is
        # simply no log_level to read. No warning needed.
        return _DEFAULT_LEVEL

    # ------------------------------------------------------------------
    # Step 3: Read the optional `log_level` key.
    # ------------------------------------------------------------------
    # dict.get(key) returns None if the key is absent — no KeyError. Absence is
    # explicitly allowed (Requirement 4.3), so we fall back silently.
    value = raw.get("log_level")
    if value is None:
        return _DEFAULT_LEVEL

    # ------------------------------------------------------------------
    # Step 4: The value must be a string.
    # ------------------------------------------------------------------
    # PYTHON QUIRK: bool is a SUBCLASS of int, but NOT of str, so a YAML boolean
    # (true/false) would fail the isinstance(str) check below and be reported as
    # a bool — which is exactly what we want. We still call it out explicitly to
    # match the project's careful handling of the bool-is-int trap elsewhere.
    if isinstance(value, bool) or not isinstance(value, str):
        _warn(f"Invalid log_level type in '{config_path}': "
              f"{type(value).__name__}. Defaulting to {_DEFAULT_LEVEL}.")
        return _DEFAULT_LEVEL

    # ------------------------------------------------------------------
    # Step 5: Normalise case and validate membership.
    # ------------------------------------------------------------------
    # str.upper() uppercases the whole string; .strip() removes stray
    # whitespace a human might leave in the YAML (e.g. "  info ").
    normalised = value.strip().upper()

    if normalised in _VALID_LEVELS:
        return normalised

    _warn(f"Unrecognised log_level '{value}' in '{config_path}'. "
          f"Valid values: {', '.join(sorted(_VALID_LEVELS))}. "
          f"Defaulting to {_DEFAULT_LEVEL}.")
    return _DEFAULT_LEVEL


def _warn(message: str) -> None:
    """
    Write a WARNING to stderr WITHOUT going through the logging framework.

    load_log_level() often runs during early module initialisation — possibly
    before file logging is initialised — so we deliberately avoid the logging
    system here and write a plain JSON-shaped line directly to sys.stderr,
    consistent with the project's structured-stderr convention.

    Args:
        message: Human-readable description of the fallback reason.
    """
    # We keep this minimal and dependency-free on purpose. It is not the hot
    # path (config is read once at startup), so a direct write is fine.
    sys.stderr.write(
        '{"event": "log_level_fallback", "level": "WARNING", '
        f'"module": "log_system", "message": {_json_str(message)}}}\n'
    )


def _json_str(text: str) -> str:
    """
    Return `text` as a JSON string literal (with surrounding quotes and any
    needed escaping). Small helper so _warn() produces valid JSON even if the
    message contains quotes or backslashes (e.g. a Windows path).
    """
    import json  # local import — only needed by this tiny helper
    return json.dumps(text)
