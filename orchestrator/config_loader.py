# orchestrator/config_loader.py
#
# Reads orchestrator/config.yaml, validates every field, and returns a typed
# OrchestratorConfig dataclass.  No other module should touch the raw YAML
# dict — everything goes through load_config().
#
# This follows the same explicit, step-by-step pattern as
# risk_engine/config_loader.py and execution/config_loader.py.
#
# Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 12.2

import logging                     # Python standard logging module
import os                          # os.path.exists — standard library path utilities
from dataclasses import dataclass  # @dataclass auto-generates __init__, __repr__, __eq__

import yaml                        # PyYAML — install with: pip install pyyaml


# Module-level logger.  Using "orchestrator" as the name is consistent with
# the project convention (each module gets a logger named after itself).
logger = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# OrchestratorConfig dataclass
# ---------------------------------------------------------------------------
#
# @dataclass is a DECORATOR — a Python concept where a function (the decorator)
# wraps a class and adds behaviour automatically at definition time.
# Here, @dataclass reads the class body and auto-generates:
#   __init__(self, bar_check_interval_seconds, symbol, state_file_path)
#   __repr__(self)   — human-readable string for print() / debugger
#   __eq__(self, other) — field-by-field equality comparison
#
# This is analogous to a C++ struct where you would manually write those same
# member functions.  With @dataclass you get them for free.
@dataclass
class OrchestratorConfig:
    """
    Validated configuration for the orchestrator module.

    Fields:
        bar_check_interval_seconds — Polling interval in seconds; float in [60, 3600].
        symbol                     — Ticker to trade; string of 1–10 chars, default "AAPL".
        state_file_path            — Path to state.json; string of 1–255 chars,
                                     default "orchestrator/state.json".
        kill_switch_file_path      — Path to the kill switch file; string of 1–255 chars,
                                     default "orchestrator/KILL_SWITCH".
    """
    bar_check_interval_seconds: float  # [60, 3600] — polling interval
    symbol: str                         # 1–10 chars, default "AAPL"
    state_file_path: str                # 1–255 chars, default "orchestrator/state.json"
    kill_switch_file_path: str          # 1–255 chars, default "orchestrator/KILL_SWITCH"


# ---------------------------------------------------------------------------
# Default values for optional keys
# ---------------------------------------------------------------------------
# These are defined as module-level constants so they can be referenced in
# tests without duplicating magic strings.

_DEFAULT_SYMBOL = "AAPL"
_DEFAULT_STATE_FILE_PATH = "orchestrator/state.json"
_DEFAULT_KILL_SWITCH_FILE_PATH = "orchestrator/KILL_SWITCH"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> OrchestratorConfig:
    """
    Read and validate the YAML file at config_path.

    Algorithm (mirrors risk_engine/config_loader.py pattern):
      Step 1 — file existence check       → FileNotFoundError
      Step 2 — YAML parsing               → ValueError on syntax error
      Step 3 — non-dict YAML check        → ValueError if parsed to non-dict
      Step 4 — required key presence check → ValueError listing missing keys
      Step 5 — value type and range check  → ValueError identifying invalid key
      Step 6 — warn on unknown keys        → WARNING log, no error
      Step 7 — apply defaults for optional keys
      Step 8 — return OrchestratorConfig(...)

    Args:
        config_path: Absolute or relative path to config.yaml.

    Returns:
        Validated OrchestratorConfig dataclass instance.

    Raises:
        FileNotFoundError: If the file does not exist at config_path.
        ValueError:        If YAML is invalid, non-dict, missing keys,
                           or any value fails a validation rule.
    """
    # ------------------------------------------------------------------
    # Step 1: File existence check
    # ------------------------------------------------------------------
    # os.path.exists() returns a bool — True if the path points to a real
    # file or directory, False otherwise.
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Config file not found: '{config_path}'. "
            f"Expected a config.yaml at that path."
        )

    # ------------------------------------------------------------------
    # Step 2: Open and parse YAML
    # ------------------------------------------------------------------
    # 'with open(...) as f:' is a CONTEXT MANAGER — Python's equivalent of
    # RAII in C++.  The 'with' block guarantees the file handle is closed
    # when the block exits (even if an exception is raised), just like a
    # C++ destructor closing the file.
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            # yaml.safe_load() parses the YAML text into a plain Python dict.
            # 'safe_load' forbids arbitrary Python object construction from
            # YAML — a security best-practice.
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        # yaml.YAMLError is the base exception for all PyYAML parse errors.
        # We convert it to ValueError so callers only need to catch ValueError
        # for data-validation problems.
        raise ValueError(f"Invalid YAML in '{config_path}': {exc}") from exc

    # ------------------------------------------------------------------
    # Step 3: Non-dict check
    # ------------------------------------------------------------------
    # yaml.safe_load() returns None for an empty file, a list if the YAML
    # root is a sequence, a string if it's a bare scalar, etc.  We require
    # a mapping (Python dict) as the top-level structure.
    if not isinstance(raw, dict):
        raise ValueError(
            f"Configuration in '{config_path}' must be a YAML mapping (dict), "
            f"but got {type(raw).__name__}."
        )

    # ------------------------------------------------------------------
    # Step 4: Required key presence check
    # ------------------------------------------------------------------
    # Only bar_check_interval_seconds is required.  symbol and state_file_path
    # are optional and will use defaults if absent (Step 7).
    required_keys = ["bar_check_interval_seconds"]

    # List comprehension — builds a new list by iterating over required_keys
    # and keeping only those that are absent from the raw dict.
    missing_keys = [key for key in required_keys if key not in raw]

    if missing_keys:
        raise ValueError(
            f"Missing required key(s) in '{config_path}': "
            f"{', '.join(missing_keys)}"
        )

    # ------------------------------------------------------------------
    # Step 5: Value type and range validation
    # ------------------------------------------------------------------

    # --- bar_check_interval_seconds: int or float (not bool), range [60, 3600] ---
    value = raw["bar_check_interval_seconds"]

    # IMPORTANT PYTHON QUIRK: In Python, bool is a SUBCLASS of int (and
    # by extension, isinstance(True, (int, float)) is True).  We must
    # check for bool FIRST and reject it, otherwise True (== 1) or False
    # (== 0) would silently pass the numeric checks.
    if isinstance(value, bool):
        raise ValueError(
            f"Invalid value for 'bar_check_interval_seconds': {value}. "
            f"Must be a number in range [60, 3600], got a boolean."
        )

    # Accept both int and float — e.g. writing "300" in YAML gives int 300.
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Invalid value for 'bar_check_interval_seconds': {value}. "
            f"Must be a number in range [60, 3600], got {type(value).__name__}."
        )

    if value < 60 or value > 3600:
        raise ValueError(
            f"Invalid value for 'bar_check_interval_seconds': {value}. "
            f"Must be in range [60, 3600]."
        )

    # --- symbol (optional): non-empty string, 1–10 characters ---
    if "symbol" in raw:
        sym_value = raw["symbol"]

        if not isinstance(sym_value, str):
            raise ValueError(
                f"Invalid value for 'symbol': {sym_value!r}. "
                f"Must be a non-empty string of 1–10 characters, "
                f"got {type(sym_value).__name__}."
            )

        if len(sym_value) < 1 or len(sym_value) > 10:
            raise ValueError(
                f"Invalid value for 'symbol': {sym_value!r}. "
                f"Must be a non-empty string of 1–10 characters "
                f"(got length {len(sym_value)})."
            )

    # --- state_file_path (optional): non-empty string, 1–255 characters ---
    if "state_file_path" in raw:
        sfp_value = raw["state_file_path"]

        if not isinstance(sfp_value, str):
            raise ValueError(
                f"Invalid value for 'state_file_path': {sfp_value!r}. "
                f"Must be a non-empty string of 1–255 characters, "
                f"got {type(sfp_value).__name__}."
            )

        if len(sfp_value) < 1 or len(sfp_value) > 255:
            raise ValueError(
                f"Invalid value for 'state_file_path': {sfp_value!r}. "
                f"Must be a non-empty string of 1–255 characters "
                f"(got length {len(sfp_value)})."
            )

    # --- kill_switch_file_path (optional): non-empty string, 1–255 characters ---
    if "kill_switch_file_path" in raw:
        ksfp_value = raw["kill_switch_file_path"]

        if not isinstance(ksfp_value, str):
            raise ValueError(
                f"Invalid value for 'kill_switch_file_path': {ksfp_value!r}. "
                f"Must be a non-empty string of 1–255 characters, "
                f"got {type(ksfp_value).__name__}."
            )

        if len(ksfp_value) < 1 or len(ksfp_value) > 255:
            raise ValueError(
                f"Invalid value for 'kill_switch_file_path': {ksfp_value!r}. "
                f"Must be a non-empty string of 1–255 characters "
                f"(got length {len(ksfp_value)})."
            )

    # ------------------------------------------------------------------
    # Step 6: Warn on unknown keys
    # ------------------------------------------------------------------
    # Any key not in our known set gets a WARNING log — no error raised.
    # This is intentionally permissive: future config additions won't
    # break older code, and typos become visible in the log.
    known_keys = {"bar_check_interval_seconds", "symbol", "state_file_path",
                  "kill_switch_file_path"}

    # set(raw.keys()) - known_keys gives us a set of unrecognized keys.
    # In Python, sets support subtraction (-) to compute the difference,
    # like std::set_difference in C++.
    unknown_keys = set(raw.keys()) - known_keys

    for key in sorted(unknown_keys):
        logger.warning(
            f"Unknown configuration key '{key}' in '{config_path}' — ignoring."
        )

    # ------------------------------------------------------------------
    # Step 7: Apply defaults for optional keys
    # ------------------------------------------------------------------
    # dict.get(key, default) returns the value if the key exists, otherwise
    # the default.  This is analogous to std::map::find + fallback in C++.
    symbol = raw.get("symbol", _DEFAULT_SYMBOL)
    state_file_path = raw.get("state_file_path", _DEFAULT_STATE_FILE_PATH)
    kill_switch_file_path = raw.get("kill_switch_file_path", _DEFAULT_KILL_SWITCH_FILE_PATH)

    # ------------------------------------------------------------------
    # Step 8: Return the validated OrchestratorConfig
    # ------------------------------------------------------------------
    return OrchestratorConfig(
        bar_check_interval_seconds=float(raw["bar_check_interval_seconds"]),
        symbol=symbol,
        state_file_path=state_file_path,
        kill_switch_file_path=kill_switch_file_path,
    )
