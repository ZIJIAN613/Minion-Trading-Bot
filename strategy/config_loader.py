# strategy/config_loader.py
#
# Reads strategy/config.yaml, validates every field, and returns a typed
# StrategyConfig dataclass.  No other module should touch the raw YAML
# dict — everything goes through load_config().
#
# This follows the same explicit, step-by-step pattern as
# data_ingestion/config_loader.py.

import os                          # os.path.exists — standard library path utilities
from dataclasses import dataclass  # @dataclass auto-generates __init__, __repr__, __eq__

import yaml                        # PyYAML — install with: pip install pyyaml


# ---------------------------------------------------------------------------
# StrategyConfig dataclass
# ---------------------------------------------------------------------------
#
# @dataclass is a DECORATOR — a Python concept where a function (the decorator)
# wraps a class and adds behaviour automatically at definition time.
# Here, @dataclass reads the class body and auto-generates:
#   __init__(self, short_window, long_window)
#   __repr__(self)   — human-readable string for print() / debugger
#   __eq__(self, other) — field-by-field equality comparison
#
# This is analogous to a C++ struct where you would manually write those same
# member functions.  With @dataclass you get them for free.
@dataclass
class StrategyConfig:
    """
    Validated configuration for the strategy module.

    Fields:
        short_window — SMA short window size; integer in [1, 10000],
                       must be strictly less than long_window.
        long_window  — SMA long window size; integer in [1, 10000],
                       must be strictly greater than short_window.
    """
    short_window: int   # e.g. 10
    long_window: int    # e.g. 30


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> StrategyConfig:
    """
    Read and validate the YAML file at config_path.

    Algorithm (mirrors data_ingestion/config_loader.py pattern):
      Step 1 — file existence check       → FileNotFoundError
      Step 2 — YAML parsing               → ValueError on syntax error
      Step 3 — required key presence check → ValueError listing missing keys
      Step 4 — value type and range check  → ValueError identifying invalid key
      Step 5 — relationship constraint     → ValueError (short < long)
      Step 6 — return StrategyConfig(...)

    Args:
        config_path: Absolute or relative path to config.yaml.

    Returns:
        Validated StrategyConfig dataclass instance.

    Raises:
        FileNotFoundError: If the file does not exist at config_path.
        ValueError:        If YAML is invalid, required keys are missing,
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

    # yaml.safe_load() returns None when the file is completely empty.
    # Treat that the same as an empty dict so the missing-key check below
    # catches it gracefully.
    if raw is None:
        raw = {}

    # ------------------------------------------------------------------
    # Step 3: Required key presence check
    # ------------------------------------------------------------------
    # Collect ALL missing keys before raising, so the error message tells
    # the developer everything at once (rather than one key at a time).
    required_keys = ["short_window", "long_window"]

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
    # Step 4: Value type and range validation
    # ------------------------------------------------------------------
    # Validate each window value individually.  We check both keys using
    # the same logic in a loop to avoid code duplication.
    for key in required_keys:
        value = raw[key]

        # IMPORTANT PYTHON QUIRK: In Python, bool is a SUBCLASS of int.
        # This means isinstance(True, int) returns True!  We must check
        # for bool FIRST and reject it, otherwise True (== 1) or False
        # (== 0) would pass the integer check silently.
        if isinstance(value, bool):
            raise ValueError(
                f"Invalid value for '{key}': {value}. "
                f"Must be an integer in the range [1, 10000], got a boolean."
            )

        # Now check that the value is actually an integer.
        if not isinstance(value, int):
            raise ValueError(
                f"Invalid value for '{key}': {value}. "
                f"Must be an integer in the range [1, 10000], "
                f"got {type(value).__name__}."
            )

        # Range check — both bounds are inclusive.
        if value < 1 or value > 10000:
            raise ValueError(
                f"Invalid value for '{key}': {value}. "
                f"Must be an integer in the range [1, 10000]."
            )

    # ------------------------------------------------------------------
    # Step 5: Relationship constraint — short_window < long_window
    # ------------------------------------------------------------------
    short_val = raw["short_window"]
    long_val = raw["long_window"]

    if short_val >= long_val:
        raise ValueError(
            f"'short_window' ({short_val}) must be strictly less than "
            f"'long_window' ({long_val})."
        )

    # ------------------------------------------------------------------
    # Step 6: Return the validated StrategyConfig dataclass
    # ------------------------------------------------------------------
    # Because @dataclass generated __init__ for us, this call is equivalent
    # to: StrategyConfig.__init__(self, short_window=short_val, long_window=long_val)
    return StrategyConfig(
        short_window=short_val,
        long_window=long_val,
    )
