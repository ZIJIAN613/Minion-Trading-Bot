# risk_engine/config_loader.py
#
# Reads risk_engine/config.yaml, validates every field, and returns a typed
# RiskConfig dataclass.  No other module should touch the raw YAML dict —
# everything goes through load_config().
#
# This follows the same explicit, step-by-step pattern as
# strategy/config_loader.py.

import os                          # os.path.exists — standard library path utilities
from dataclasses import dataclass  # @dataclass auto-generates __init__, __repr__, __eq__

import yaml                        # PyYAML — install with: pip install pyyaml


# ---------------------------------------------------------------------------
# RiskConfig dataclass
# ---------------------------------------------------------------------------
#
# @dataclass is a DECORATOR — a Python concept where a function (the decorator)
# wraps a class and adds behaviour automatically at definition time.
# Here, @dataclass reads the class body and auto-generates:
#   __init__(self, max_position_percent, max_daily_loss, max_open_positions, stop_loss_percent)
#   __repr__(self)   — human-readable string for print() / debugger
#   __eq__(self, other) — field-by-field equality comparison
#
# This is analogous to a C++ struct where you would manually write those same
# member functions.  With @dataclass you get them for free.
@dataclass
class RiskConfig:
    """
    Validated configuration for the risk engine module.

    Fields:
        max_position_percent — Maximum % of equity per trade; float in (0, 100) exclusive.
        max_daily_loss       — Maximum realized loss in USD per day; positive float.
        max_open_positions   — Maximum concurrent open positions; int in [1, 1000].
        stop_loss_percent    — % distance from entry for stop-loss; float in (0, 100) exclusive.
    """
    max_position_percent: float  # (0, 100) exclusive — max % of equity per trade
    max_daily_loss: float        # positive float — max realized loss in USD per day
    max_open_positions: int      # [1, 1000] — max concurrent open positions
    stop_loss_percent: float     # (0, 100) exclusive — % distance for stop-loss


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> RiskConfig:
    """
    Read and validate the YAML file at config_path.

    Algorithm (mirrors strategy/config_loader.py pattern):
      Step 1 — file existence check       → FileNotFoundError
      Step 2 — YAML parsing               → ValueError on syntax error
      Step 3 — non-dict YAML check        → ValueError if parsed to non-dict
      Step 4 — required key presence check → ValueError listing missing keys
      Step 5 — value type and range check  → ValueError identifying invalid key
      Step 6 — return RiskConfig(...)

    Args:
        config_path: Absolute or relative path to config.yaml.

    Returns:
        Validated RiskConfig dataclass instance.

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
    # Collect ALL missing keys before raising, so the error message tells
    # the developer everything at once (rather than one key at a time).
    required_keys = [
        "max_position_percent",
        "max_daily_loss",
        "max_open_positions",
        "stop_loss_percent",
    ]

    # List comprehension — builds a new list by iterating over required_keys
    # and keeping only those that are absent from the raw dict.
    # Equivalent C++ pseudo-code:
    #   vector<string> missing;
    #   for (auto& key : required_keys)
    #       if (raw.find(key) == raw.end()) missing.push_back(key);
    missing_keys = [key for key in required_keys if key not in raw]

    if missing_keys:
        # ', '.join(iterable) — concatenates list items with ', ' as separator.
        raise ValueError(
            f"Missing required key(s) in '{config_path}': "
            f"{', '.join(missing_keys)}"
        )

    # ------------------------------------------------------------------
    # Step 5: Value type and range validation
    # ------------------------------------------------------------------
    # Each key has its own type and range constraints.  We validate them
    # one-by-one with explicit checks and clear error messages.

    # --- max_position_percent: float (not bool), range (0, 100) exclusive ---
    value = raw["max_position_percent"]

    # IMPORTANT PYTHON QUIRK: In Python, bool is a SUBCLASS of int (and
    # by extension, isinstance(True, (int, float)) is True).  We must
    # check for bool FIRST and reject it, otherwise True (== 1) or False
    # (== 0) would silently pass the numeric checks.
    if isinstance(value, bool):
        raise ValueError(
            f"Invalid value for 'max_position_percent': {value}. "
            f"Must be a float in range (0, 100), got a boolean."
        )

    # Accept both int and float — e.g. writing "5" in YAML gives int 5,
    # which is a valid percentage.  But NOT bool (already rejected above).
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Invalid value for 'max_position_percent': {value}. "
            f"Must be a float in range (0, 100), got {type(value).__name__}."
        )

    if value <= 0 or value >= 100:
        raise ValueError(
            f"Invalid value for 'max_position_percent': {value}. "
            f"Must be in range (0, 100) exclusive."
        )

    # --- max_daily_loss: float or int (not bool), must be > 0 ---
    value = raw["max_daily_loss"]

    if isinstance(value, bool):
        raise ValueError(
            f"Invalid value for 'max_daily_loss': {value}. "
            f"Must be a positive number, got a boolean."
        )

    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Invalid value for 'max_daily_loss': {value}. "
            f"Must be a positive number, got {type(value).__name__}."
        )

    if value <= 0:
        raise ValueError(
            f"Invalid value for 'max_daily_loss': {value}. "
            f"Must be greater than 0."
        )

    # --- max_open_positions: int only (not bool, not float), range [1, 1000] ---
    value = raw["max_open_positions"]

    if isinstance(value, bool):
        raise ValueError(
            f"Invalid value for 'max_open_positions': {value}. "
            f"Must be an integer in range [1, 1000], got a boolean."
        )

    # For this field we require exactly int — a float like 5.0 is NOT
    # acceptable because position counts must be whole numbers.
    if not isinstance(value, int):
        raise ValueError(
            f"Invalid value for 'max_open_positions': {value}. "
            f"Must be an integer in range [1, 1000], got {type(value).__name__}."
        )

    if value < 1 or value > 1000:
        raise ValueError(
            f"Invalid value for 'max_open_positions': {value}. "
            f"Must be an integer in range [1, 1000]."
        )

    # --- stop_loss_percent: float (not bool), range (0, 100) exclusive ---
    value = raw["stop_loss_percent"]

    if isinstance(value, bool):
        raise ValueError(
            f"Invalid value for 'stop_loss_percent': {value}. "
            f"Must be a float in range (0, 100), got a boolean."
        )

    # Accept both int and float (same reasoning as max_position_percent).
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Invalid value for 'stop_loss_percent': {value}. "
            f"Must be a float in range (0, 100), got {type(value).__name__}."
        )

    if value <= 0 or value >= 100:
        raise ValueError(
            f"Invalid value for 'stop_loss_percent': {value}. "
            f"Must be in range (0, 100) exclusive."
        )

    # ------------------------------------------------------------------
    # Step 6: Return the validated RiskConfig dataclass
    # ------------------------------------------------------------------
    # Because @dataclass generated __init__ for us, this call is equivalent
    # to: RiskConfig.__init__(self, max_position_percent=..., ...)
    #
    # We cast max_position_percent, max_daily_loss, and stop_loss_percent
    # to float explicitly so the dataclass fields have consistent types
    # even if the YAML provided an integer (e.g. "5" → 5.0).
    return RiskConfig(
        max_position_percent=float(raw["max_position_percent"]),
        max_daily_loss=float(raw["max_daily_loss"]),
        max_open_positions=raw["max_open_positions"],
        stop_loss_percent=float(raw["stop_loss_percent"]),
    )
