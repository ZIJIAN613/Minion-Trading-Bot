# data_ingestion/config_loader.py
#
# Reads data_ingestion/config.yaml, validates every field, and returns a
# typed Config dataclass.  No other module should touch the raw YAML dict —
# everything goes through load_config().

import os                          # os.path.exists — standard library path utilities
from dataclasses import dataclass  # @dataclass is a decorator (explained below)
from typing import List            # explicit type hints — clearer than bare 'list' on older Pythons

import yaml                        # PyYAML — install with: pip install pyyaml

# Re-use the already-validated set of accepted timeframe strings from the map
# module so there is only one source of truth.
from data_ingestion.timeframe_map import VALID_TIMEFRAMES


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------
#
# @dataclass is a DECORATOR — a Python concept where a function (the decorator)
# wraps a class or function and adds behaviour automatically at definition time.
# Here, @dataclass reads the class body and auto-generates:
#   __init__(self, symbols, timeframe, lookback_days)
#   __repr__(self)   — human-readable string for print() / debugger
#   __eq__(self, other) — field-by-field equality comparison
#
# This is analogous to a C++ struct where you would manually write those same
# member functions (or use a code generator / macro to produce them).
# With @dataclass you get them for free.
@dataclass
class Config:
    """
    Validated configuration loaded from config.yaml.

    Fields:
        symbols      — list of equity tickers, each 1–10 characters long.
        timeframe    — candle bar interval; must be one of VALID_TIMEFRAMES.
        lookback_days — how many calendar days of history to fetch; 1–365.
    """
    symbols: List[str]   # e.g. ["AAPL", "SPY"]
    timeframe: str       # e.g. "1d"
    lookback_days: int   # e.g. 90


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> Config:
    """
    Read and validate the YAML file at config_path.

    This function implements Algorithm B from the design document:
      Step 1 — file existence check   → FileNotFoundError
      Step 2 — YAML parsing           → ValueError on syntax error
      Step 3 — required key check     → ValueError listing all missing keys
      Step 4 — symbol list validation → ValueError on first bad symbol
      Step 5 — timeframe validation   → ValueError listing accepted values
      Step 6 — lookback_days validation → ValueError with accepted range
      Step 7 — return Config(...)

    Args:
        config_path: Absolute or relative path to config.yaml.

    Returns:
        Validated Config dataclass instance.

    Raises:
        FileNotFoundError: If the file does not exist at config_path.
        ValueError:        If YAML is invalid, required keys are missing,
                           or any value fails a validation rule.
    """

    # ------------------------------------------------------------------
    # Step 1: File existence check
    # ------------------------------------------------------------------
    # os.path.exists() returns a bool — True if the path points to a real
    # file or directory, False otherwise.  There is no exception to catch;
    # we check the result ourselves and raise the right error type.
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
            # 'safe_load' (as opposed to 'full_load') forbids arbitrary Python
            # object construction from YAML, which is a security best-practice.
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        # yaml.YAMLError is the base exception for all PyYAML parse errors.
        # We convert it to ValueError so callers only need to catch ValueError
        # for data-validation problems (consistent with the rest of the module).
        raise ValueError(f"Invalid YAML in '{config_path}': {exc}") from exc

    # yaml.safe_load() returns None when the file is completely empty.
    # Treat that the same as an empty dict so the missing-key check below
    # catches it gracefully.
    if raw is None:
        raw = {}

    # ------------------------------------------------------------------
    # Step 3: Required key presence check
    # ------------------------------------------------------------------
    # Collect ALL missing keys before raising so the error message tells the
    # developer everything at once (rather than forcing them to fix one key,
    # re-run, and discover the next missing key).
    required_keys = ["symbols", "timeframe", "lookback_days"]
    missing_keys = []

    # 'for key in required_keys:' is a range-based for loop — identical to
    # C++11's 'for (auto& key : required_keys)'.
    for key in required_keys:
        # dict.get(key) returns None if the key is absent (no exception).
        # We compare directly to None with 'is not in raw' to distinguish
        # a truly absent key from a key whose value happens to be None.
        if key not in raw:
            missing_keys.append(key)

    if missing_keys:
        # ', '.join(iterable) — Python string method that concatenates all
        # items in an iterable with ', ' as the separator.  Equivalent to
        # joining a vector of strings in C++.
        raise ValueError(
            f"Missing required key(s) in '{config_path}': "
            f"{', '.join(missing_keys)}"
        )

    # ------------------------------------------------------------------
    # Step 4: Symbol list validation
    # ------------------------------------------------------------------
    symbols_raw = raw["symbols"]

    # Guard against the YAML key being present but not a list (e.g. a scalar).
    if not isinstance(symbols_raw, list):
        raise ValueError(
            f"'symbols' must be a YAML list, got {type(symbols_raw).__name__}."
        )

    # Guard against an empty symbols list — at least one symbol is required.
    if len(symbols_raw) == 0:
        raise ValueError("'symbols' list must not be empty.")

    # enumerate(iterable) yields (index, value) pairs — equivalent to a
    # C++ index-based loop 'for (size_t i = 0; i < v.size(); ++i)' but
    # without manually maintaining the counter variable.
    for i, symbol in enumerate(symbols_raw):
        if (
            not isinstance(symbol, str)  # must be a string, not int/None/etc.
            or len(symbol) == 0          # must be non-empty
            or len(symbol) > 10          # must not exceed 10 characters
        ):
            raise ValueError(
                f"Invalid symbol at index {i}: '{symbol}'. "
                f"Each symbol must be a non-empty string of 1–10 characters."
            )

    # ------------------------------------------------------------------
    # Step 5: Timeframe validation
    # ------------------------------------------------------------------
    timeframe_raw = raw["timeframe"]

    # VALID_TIMEFRAMES is a frozenset, so 'in' is O(1) — same as
    # std::unordered_set::count() in C++.
    if timeframe_raw not in VALID_TIMEFRAMES:
        # sorted() returns a new list in lexicographic order — useful here
        # so the accepted-values list is deterministic across runs.
        accepted = ", ".join(sorted(VALID_TIMEFRAMES))
        raise ValueError(
            f"Invalid timeframe '{timeframe_raw}'. "
            f"Accepted values are: {accepted}."
        )

    # ------------------------------------------------------------------
    # Step 6: lookback_days validation
    # ------------------------------------------------------------------
    lookback_raw = raw["lookback_days"]

    # isinstance(x, bool) must come BEFORE isinstance(x, int) because in
    # Python, bool is a subclass of int — True == 1 and False == 0.
    # Without this guard, True/False would pass the int check, which we
    # don't want (a boolean is not a meaningful day count).
    if (
        isinstance(lookback_raw, bool)          # reject True / False
        or not isinstance(lookback_raw, int)    # must be an integer
        or lookback_raw < 1                     # lower bound (inclusive)
        or lookback_raw > 365                   # upper bound (inclusive)
    ):
        raise ValueError(
            f"Invalid lookback_days value '{lookback_raw}'. "
            f"Must be an integer in the range [1, 365]."
        )

    # ------------------------------------------------------------------
    # Step 7: Return the validated Config dataclass
    # ------------------------------------------------------------------
    # Because @dataclass generated __init__ for us, this call is equivalent
    # to: Config.__init__(symbols=symbols_raw, timeframe=timeframe_raw,
    #                     lookback_days=lookback_raw)
    return Config(
        symbols=symbols_raw,
        timeframe=timeframe_raw,
        lookback_days=lookback_raw,
    )
