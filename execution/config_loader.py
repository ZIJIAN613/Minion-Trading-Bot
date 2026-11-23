# execution/config_loader.py
#
# Reads execution/config.yaml, validates every field, and returns a typed
# ExecutionConfig dataclass.  No other module should touch the raw YAML
# dict — everything goes through load_config().
#
# This follows the same explicit, step-by-step pattern as
# risk_engine/config_loader.py.

import logging            # Python standard library — structured logging
import os                 # os.path.exists — standard library path utilities
from dataclasses import dataclass  # @dataclass auto-generates __init__, __repr__, __eq__

import yaml               # PyYAML — install with: pip install pyyaml

# Get a logger for this module.  Using the module name "execution" keeps all
# log messages from this package under a single namespace.
logger = logging.getLogger("execution")


# ---------------------------------------------------------------------------
# ExecutionConfig dataclass
# ---------------------------------------------------------------------------
#
# @dataclass is a DECORATOR — a Python concept where a function (the decorator)
# wraps a class and adds behaviour automatically at definition time.
# Here, @dataclass reads the class body and auto-generates:
#   __init__(self, fill_timeout_seconds, poll_interval_seconds)
#   __repr__(self)   — human-readable string for print() / debugger
#   __eq__(self, other) — field-by-field equality comparison
#
# This is analogous to a C++ struct where you would manually write those same
# member functions.  With @dataclass you get them for free.
@dataclass
class ExecutionConfig:
    """
    Validated configuration for the execution module.

    Fields:
        fill_timeout_seconds  — Max seconds to wait for an order to fill;
                                int or float in [5, 300].
        poll_interval_seconds — Seconds between Alpaca status polls;
                                int or float in [0.5, 30].
    """
    fill_timeout_seconds: float   # [5, 300] — max seconds to wait for fill
    poll_interval_seconds: float  # [0.5, 30] — seconds between status polls


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> ExecutionConfig:
    """
    Read and validate the YAML file at config_path.

    Algorithm (mirrors risk_engine/config_loader.py pattern):
      Step 1 — file existence check       → FileNotFoundError
      Step 2 — YAML parsing               → ValueError on syntax error
      Step 3 — non-dict YAML check        → ValueError if parsed to non-dict
      Step 4 — required key presence check → ValueError listing missing keys
      Step 5 — value type and range check  → ValueError identifying invalid key
               (booleans are explicitly rejected — see Python quirk note below)
      Step 6 — warn on unknown keys        → WARNING log, no error raised
      Step 7 — return ExecutionConfig(...)

    Args:
        config_path: Absolute or relative path to config.yaml.

    Returns:
        Validated ExecutionConfig dataclass instance.

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
        "fill_timeout_seconds",
        "poll_interval_seconds",
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
    #
    # IMPORTANT PYTHON QUIRK: In Python, bool is a SUBCLASS of int (and
    # by extension, isinstance(True, (int, float)) is True).  We MUST
    # check for bool FIRST and reject it, otherwise True (== 1) or False
    # (== 0) would silently pass the numeric checks.  This is different
    # from C++ where bool is a separate type.

    # --- fill_timeout_seconds: int or float (NOT bool), range [5, 300] ---
    value = raw["fill_timeout_seconds"]

    if isinstance(value, bool):
        raise ValueError(
            f"Invalid value for 'fill_timeout_seconds': {value}. "
            f"Must be a number in range [5, 300], got a boolean."
        )

    # Accept both int and float — e.g. writing "30" in YAML gives int 30,
    # which is a valid timeout.  But NOT bool (already rejected above).
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Invalid value for 'fill_timeout_seconds': {value}. "
            f"Must be a number in range [5, 300], got {type(value).__name__}."
        )

    if value < 5 or value > 300:
        raise ValueError(
            f"Invalid value for 'fill_timeout_seconds': {value}. "
            f"Must be in range [5, 300]."
        )

    # --- poll_interval_seconds: int or float (NOT bool), range [0.5, 30] ---
    value = raw["poll_interval_seconds"]

    if isinstance(value, bool):
        raise ValueError(
            f"Invalid value for 'poll_interval_seconds': {value}. "
            f"Must be a number in range [0.5, 30], got a boolean."
        )

    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Invalid value for 'poll_interval_seconds': {value}. "
            f"Must be a number in range [0.5, 30], got {type(value).__name__}."
        )

    if value < 0.5 or value > 30:
        raise ValueError(
            f"Invalid value for 'poll_interval_seconds': {value}. "
            f"Must be in range [0.5, 30]."
        )

    # ------------------------------------------------------------------
    # Step 6: Warn on unknown keys
    # ------------------------------------------------------------------
    # If the user added extra keys (possibly typos), we log a warning but
    # do NOT raise an error.  This helps catch mistakes without blocking
    # startup.
    #
    # set(raw.keys()) gives all keys in the parsed YAML dict.
    # set(required_keys) gives the known expected keys.
    # The '-' operator on sets computes the difference (keys in raw but
    # not in required_keys).  This is like C++ std::set_difference().
    unknown_keys = set(raw.keys()) - set(required_keys)

    if unknown_keys:
        # sorted() returns a deterministic order for the log message.
        logger.warning(
            "Unknown key(s) in '%s': %s. These will be ignored.",
            config_path,
            ", ".join(sorted(unknown_keys)),
        )

    # ------------------------------------------------------------------
    # Step 7: Return the validated ExecutionConfig dataclass
    # ------------------------------------------------------------------
    # Because @dataclass generated __init__ for us, this call is equivalent
    # to: ExecutionConfig.__init__(self, fill_timeout_seconds=..., ...)
    #
    # We cast both values to float explicitly so the dataclass fields have
    # consistent types even if the YAML provided an integer (e.g. "30" → 30.0).
    return ExecutionConfig(
        fill_timeout_seconds=float(raw["fill_timeout_seconds"]),
        poll_interval_seconds=float(raw["poll_interval_seconds"]),
    )
