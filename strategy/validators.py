# strategy/validators.py
#
# Self-contained bar DataFrame schema validation for the strategy module.
#
# IMPORTANT DESIGN DECISION: This file does NOT import from data_ingestion.
# The data_ingestion module's __init__.py pulls in Alpaca SDK classes, which
# in turn import networking libraries.  By implementing our own validation
# here using only pandas/numpy, we guarantee that importing the strategy
# module never transitively loads any network-capable code.
# (Satisfies Requirement 5.1 — zero-network-import constraint.)
#
# The validation logic mirrors data_ingestion/schemas.py's validate_bar_dataframe()
# but is completely independent — no shared code, no cross-module imports.

import pandas as pd   # DataFrame, DatetimeIndex — the core data structure library
import numpy as np    # numpy is pandas' underlying array engine (imported for clarity)


# ---------------------------------------------------------------------------
# Required columns and their expected dtypes
# ---------------------------------------------------------------------------
# A plain dict mapping column name → expected dtype string.
# This is Python's equivalent of std::unordered_map<std::string, std::string>.
# We define it here as a module-level constant so it's easy to find and update.

REQUIRED_COLUMNS: dict[str, str] = {
    "open":   "float64",   # Opening price of the bar
    "high":   "float64",   # Highest price during the bar
    "low":    "float64",   # Lowest price during the bar
    "close":  "float64",   # Closing price of the bar
    "volume": "int64",     # Total shares traded during the bar
}


# ---------------------------------------------------------------------------
# validate_bar_dataframe
# ---------------------------------------------------------------------------

def validate_bar_dataframe(df: pd.DataFrame) -> None:
    """
    Validate that a DataFrame conforms to the Bar_DataFrame canonical schema.

    This is an independent implementation within the strategy module — it does
    NOT import from data_ingestion. This avoids transitively loading Alpaca SDK
    classes (which data_ingestion/__init__.py pulls in), satisfying
    Requirement 5.1's zero-network-import constraint.

    Checks (in order — stops at first failure):
      1. Index is a DatetimeIndex.
      2. Index timezone is UTC.
      3. Required columns exist: open, high, low, close, volume.
      4. Column dtypes match: open/high/low/close are float64, volume is int64.

    This function does NOT check for:
      - Whether df is a DataFrame (caller's responsibility — compute_signal
        does the TypeError check before calling this function).
      - NaN values in columns (separate check in compute_signal).
      - Sort order of the index (separate check in compute_signal).
      - Empty DataFrame / row count (separate check in compute_signal).

    Args:
        df: The DataFrame to validate. Assumed to already be confirmed as a
            pandas DataFrame by the caller.

    Raises:
        ValueError: If any schema check fails. The message identifies the
                    first mismatch found (fail-fast, like a C++ assert).
    """

    # ------------------------------------------------------------------
    # Step 1: Check that the index is a DatetimeIndex
    # ------------------------------------------------------------------
    # isinstance(x, T) is Python's equivalent of dynamic_cast<T*> — returns
    # True only if x is of type T or a subclass of T.
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(
            f"Bar DataFrame index must be a DatetimeIndex, "
            f"but got {type(df.index).__name__}"
        )

    # ------------------------------------------------------------------
    # Step 2: Check that the index timezone is UTC
    # ------------------------------------------------------------------
    # DatetimeIndex.tz is None when no timezone is attached (timezone-naive).
    # We compare the string representation so we accept both pytz.UTC and
    # datetime.timezone.utc — both display as "UTC" when converted to str.
    if df.index.tz is None or str(df.index.tz) != "UTC":
        actual_tz = (
            str(df.index.tz) if df.index.tz is not None
            else "None (timezone-naive)"
        )
        raise ValueError(
            f"Bar DataFrame index must have UTC timezone, "
            f"but got timezone: {actual_tz}"
        )

    # ------------------------------------------------------------------
    # Step 3: Check that all required columns are present
    # ------------------------------------------------------------------
    # set(REQUIRED_COLUMNS.keys()) builds a set of the column names we need.
    # set(df.columns) builds a set of the column names actually in the DataFrame.
    # The '-' operator computes the set difference: items in the first set
    # that are NOT in the second.  This is like C++:
    #   std::set_difference(required.begin(), required.end(),
    #                       actual.begin(), actual.end(), ...);
    required_names = set(REQUIRED_COLUMNS.keys())
    actual_names = set(df.columns)
    missing = required_names - actual_names

    if missing:
        # sorted() ensures a deterministic error message regardless of
        # Python's internal set ordering.
        raise ValueError(
            f"Bar DataFrame is missing required column(s): "
            f"{sorted(missing)}"
        )

    # ------------------------------------------------------------------
    # Step 4: Check column dtypes
    # ------------------------------------------------------------------
    # We iterate over REQUIRED_COLUMNS in insertion order (Python dicts
    # preserve insertion order since Python 3.7). This makes the error
    # message deterministic — if multiple columns have wrong dtypes, we
    # always report the first one in our canonical order.
    #
    # .items() yields (key, value) pairs — like range-based for over a
    # std::map in C++, giving both the key and value in one step.
    for col_name, expected_dtype in REQUIRED_COLUMNS.items():
        actual_dtype = str(df[col_name].dtype)
        if actual_dtype != expected_dtype:
            raise ValueError(
                f"Bar DataFrame column '{col_name}' has wrong dtype: "
                f"expected '{expected_dtype}', got '{actual_dtype}'"
            )
