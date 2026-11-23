# data_ingestion/schemas.py
#
# Defines the canonical DataFrame schemas for this module and provides
# validation helpers used by RestClient and StreamClient before any
# DataFrame leaves the module.
#
# Two schemas are defined here:
#   BAR_COLUMNS   — historical OHLCV candle bars (UTC DatetimeIndex + 5 columns)
#   QUOTE_COLUMNS — live WebSocket quotes (5 columns, no special index)
#
# Requirements: 5.1, 5.2, 5.3

import pandas as pd


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

# BAR_COLUMNS maps each column name to its expected pandas dtype string.
# This is a plain dict — Python's equivalent of std::unordered_map<std::string, std::string>.
# The order here also defines the canonical column order for empty DataFrames.
#
# In addition to these columns, bar DataFrames must have a DatetimeIndex
# with UTC timezone (checked separately in validate_bar_dataframe below).
BAR_COLUMNS: dict[str, str] = {
    "open":   "float64",   # Opening price of the bar
    "high":   "float64",   # Highest price during the bar
    "low":    "float64",   # Lowest price during the bar
    "close":  "float64",   # Closing price of the bar
    "volume": "int64",     # Total shares traded during the bar
}

# QUOTE_COLUMNS maps each column name to its expected pandas dtype string.
# Quote DataFrames always have exactly one row (one incoming message).
#
# Note on "str" vs "object": prior to pandas 3.0, string columns had dtype
# "object" (numpy's generic Python-object container, like std::variant<>).
# Pandas 3.0 introduced a dedicated StringDtype whose str() representation is
# "str". We use "str" here to match the runtime behaviour of pandas 3.x.
# If you downgrade to pandas < 3.0, change this back to "object".
QUOTE_COLUMNS: dict[str, str] = {
    "symbol":    "str",                 # Ticker string, e.g. "AAPL" (StringDtype in pandas 3.x)
    "bid_price": "float64",             # Current best bid price
    "ask_price": "float64",             # Current best ask price
    "mid_price": "float64",             # (bid + ask) / 2, computed in-module
    "timestamp": "datetime64[ns, UTC]", # Exchange timestamp, UTC-aware
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_bar_dataframe(df: pd.DataFrame) -> None:
    """
    Assert that df conforms to the bar schema.

    Checks in order:
      1. The index is a DatetimeIndex (not a plain integer or string index).
      2. The index is timezone-aware and set to UTC.
      3. Each column in BAR_COLUMNS exists with the correct dtype.

    We stop and raise on the FIRST problem found — this mirrors how a C++
    assert() fires: one clear error, no ambiguity about which check failed.

    Args:
        df: The DataFrame to validate.

    Raises:
        ValueError: Describing the first mismatch found. The message always
                    names what was wrong so the caller can fix it quickly.
    """

    # --- Check 1: index must be a DatetimeIndex ---
    # isinstance(x, T) is Python's equivalent of dynamic_cast<T*> — it returns
    # True only if x is of type T (or a subclass of T).
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(
            f"Bar DataFrame index must be a DatetimeIndex, "
            f"but got {type(df.index).__name__}"
        )

    # --- Check 2: index must be timezone-aware and set to UTC ---
    # DatetimeIndex.tz is None when no timezone is attached (timezone-naive).
    # We compare the string representation so we accept both pytz.UTC and
    # datetime.timezone.utc, both of which display as "UTC".
    if df.index.tz is None or str(df.index.tz) != "UTC":
        actual_tz = str(df.index.tz) if df.index.tz is not None else "None (timezone-naive)"
        raise ValueError(
            f"Bar DataFrame index must have UTC timezone, "
            f"but got timezone: {actual_tz}"
        )

    # --- Check 3: validate each column name and dtype ---
    # We iterate over BAR_COLUMNS in insertion order (Python dicts preserve
    # insertion order since Python 3.7) so the error message is deterministic
    # and matches the canonical column order defined above.
    #
    # .items() yields (key, value) pairs — like range-based for over a
    # std::map in C++, giving us both the key and the value in one step.
    for col_name, expected_dtype in BAR_COLUMNS.items():
        # Check that the column exists at all.
        # 'col_name in df.columns' is O(1) — df.columns is an Index object
        # backed by a hash set internally.
        if col_name not in df.columns:
            raise ValueError(
                f"Bar DataFrame is missing required column '{col_name}' "
                f"(expected dtype: {expected_dtype})"
            )

        # Check that the column has the correct dtype.
        # str(df[col_name].dtype) gives us the canonical string like "float64"
        # or "int64" — comparable directly to our schema constant strings.
        actual_dtype = str(df[col_name].dtype)
        if actual_dtype != expected_dtype:
            raise ValueError(
                f"Bar DataFrame column '{col_name}' has wrong dtype: "
                f"expected '{expected_dtype}', got '{actual_dtype}'"
            )


def validate_quote_dataframe(df: pd.DataFrame) -> None:
    """
    Assert that df conforms to the quote schema.

    Checks each column in QUOTE_COLUMNS for existence and correct dtype.
    Raises on the FIRST mismatch found.

    Note: quote DataFrames use a plain integer RangeIndex (no special index
    requirement), so there is no index check here.

    Args:
        df: The DataFrame to validate.

    Raises:
        ValueError: Describing the first mismatch found (column name,
                    expected dtype, actual dtype).
    """

    # Iterate QUOTE_COLUMNS in insertion order — same pattern as the bar
    # validator above. This guarantees a deterministic error message.
    for col_name, expected_dtype in QUOTE_COLUMNS.items():
        # Check that the column exists.
        if col_name not in df.columns:
            raise ValueError(
                f"Quote DataFrame is missing required column '{col_name}' "
                f"(expected dtype: {expected_dtype})"
            )

        # Check the dtype.
        actual_dtype = str(df[col_name].dtype)
        if actual_dtype != expected_dtype:
            raise ValueError(
                f"Quote DataFrame column '{col_name}' has wrong dtype: "
                f"expected '{expected_dtype}', got '{actual_dtype}'"
            )


# ---------------------------------------------------------------------------
# Empty DataFrame factory
# ---------------------------------------------------------------------------

def make_empty_bar_dataframe() -> pd.DataFrame:
    """
    Return a zero-row DataFrame with the correct bar schema.

    Used by RestClient when the Alpaca API returns no bars for the requested
    date range — the caller always gets a well-typed DataFrame, never None.

    Returns:
        A DataFrame with:
          - columns: open (float64), high (float64), low (float64),
                     close (float64), volume (int64)
          - index:   UTC-aware DatetimeIndex with zero entries
          - rows:    0
    """

    # pd.DataFrame(columns=list_of_names) creates a zero-row DataFrame where
    # every column has dtype 'object' by default.  We then call .astype(dict)
    # to set each column's dtype explicitly.
    #
    # .astype() on an empty frame sets column dtypes without needing any rows
    # — handy Python/pandas pattern: the dtype metadata is stored independently
    # of the data, so we can cast an empty column just like a full one.
    df = pd.DataFrame(columns=list(BAR_COLUMNS.keys())).astype(BAR_COLUMNS)
    # list(BAR_COLUMNS.keys()) produces ["open", "high", "low", "close", "volume"]
    # — list() here converts the dict_keys view to a plain list, which
    # pd.DataFrame's columns parameter expects.

    # Attach a UTC-aware DatetimeIndex with zero entries.
    # pd.DatetimeIndex([]) creates an empty index; tz_localize("UTC") stamps
    # it as UTC-aware — the same timezone requirement that validate_bar_dataframe
    # enforces.  This ensures the empty frame passes validation just like a
    # frame with real data.
    df.index = pd.DatetimeIndex([]).tz_localize("UTC")
    df.index.name = "timestamp"  # Match the label used by the Alpaca SDK

    return df
