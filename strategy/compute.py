# strategy/compute.py
#
# Core signal computation for the strategy module.
#
# This file contains compute_signal() — the single public entry point that
# takes a bar DataFrame and a StrategyConfig, validates inputs, computes
# short and long Simple Moving Averages (SMAs), detects crossover events,
# and returns a structured signal dictionary.
#
# DESIGN PRINCIPLE: This function is PURE — no side effects, no I/O, no
# network calls, no mutable global state.  Same inputs always produce the
# same outputs (determinism).
#
# IMPORTS: Only pandas, numpy, and our own strategy sub-modules.
# NO networking imports, NO subprocess, NO alpaca SDK.

import pandas as pd   # DataFrame manipulation — the core data structure library
import numpy as np    # Numerical operations — pandas' underlying array engine

# Import from our own strategy package:
from strategy.config_loader import StrategyConfig   # The validated config dataclass
from strategy.validators import validate_bar_dataframe  # Schema validation


# ---------------------------------------------------------------------------
# compute_signal
# ---------------------------------------------------------------------------

def compute_signal(
    df: pd.DataFrame,
    config: StrategyConfig,
    symbol: str | None = None,
) -> dict:
    """
    Compute an SMA crossover signal from a bar DataFrame.

    This function follows a strict validation-then-computation pipeline.
    Validation checks run in a defined order — the first failure raises
    immediately (fail-fast), so no partial computation ever occurs.

    Algorithm:
      Step 1 — Type check: verify df is a pandas DataFrame
      Step 2 — Schema validation: UTC DatetimeIndex + OHLCV columns
      Step 3 — Empty check: at least one row required
      Step 4 — NaN check: no NaN values in the close column
      Step 5 — Sort check: index must be strictly ascending (no duplicates)
      Step 6 — Symbol validation: if provided, must be non-empty str ≤ 10 chars
      Step 7 — Insufficient data: early return HOLD if not enough rows
      Step 8 — SMA computation: short and long SMAs for current and previous bar
      Step 9 — Crossover detection: BUY / SELL / HOLD logic
      Step 10 — Build and return result dictionary

    Args:
        df:     A bar DataFrame with UTC DatetimeIndex and OHLCV columns.
        config: A validated StrategyConfig specifying window sizes.
        symbol: Optional ticker symbol string (1–10 characters) to include
                in the output dictionary.

    Returns:
        A dictionary with keys:
          - "signal":    one of "BUY", "SELL", or "HOLD"
          - "short_sma": float (current short SMA) or None if insufficient data
          - "long_sma":  float (current long SMA) or None if insufficient data
          - "timestamp": pandas Timestamp (UTC-aware) from the last row
          - "symbol":    (only if a valid symbol was provided)

    Raises:
        TypeError:  If df is not a pandas DataFrame.
        ValueError: If schema validation fails, close has NaN, index is
                    unsorted, DataFrame is empty, or symbol is invalid.
    """

    # ------------------------------------------------------------------
    # Step 1: Type check — verify df is a pandas DataFrame
    # ------------------------------------------------------------------
    # isinstance(x, T) checks whether x is of type T or a subclass of T.
    # This is Python's equivalent of dynamic_cast in C++.
    if not isinstance(df, pd.DataFrame):
        # type(df).__name__ gives the human-readable class name (e.g. "list",
        # "NoneType", "int") — similar to typeid(x).name() in C++.
        raise TypeError(
            f"Expected a pandas DataFrame, but received {type(df).__name__}"
        )

    # ------------------------------------------------------------------
    # Step 2: Schema validation — UTC DatetimeIndex + OHLCV columns
    # ------------------------------------------------------------------
    # Delegates to our independent validator (no data_ingestion imports).
    # Raises ValueError on first schema mismatch found.
    validate_bar_dataframe(df)

    # ------------------------------------------------------------------
    # Step 3: Empty DataFrame check
    # ------------------------------------------------------------------
    # Must come BEFORE NaN/sort checks — can't inspect NaN or sort order
    # on a DataFrame with zero rows.
    if len(df) == 0:
        raise ValueError(
            "DataFrame must contain at least one row"
        )

    # ------------------------------------------------------------------
    # Step 4: NaN check on the close column
    # ------------------------------------------------------------------
    # .isna() returns a boolean Series (True where value is NaN).
    # .sum() counts the True values — equivalent to std::count_if in C++.
    nan_count = df["close"].isna().sum()
    if nan_count > 0:
        raise ValueError(
            f"The 'close' column contains {nan_count} NaN value(s)"
        )

    # ------------------------------------------------------------------
    # Step 5: Sort check — DatetimeIndex must be strictly ascending
    # ------------------------------------------------------------------
    # "Strictly ascending" means two conditions must BOTH hold:
    #   1. is_monotonic_increasing — each timestamp >= the previous one
    #   2. is_unique — no two timestamps are equal (no duplicates)
    # Together these guarantee every timestamp is strictly > the previous.
    #
    # This is analogous to checking that a C++ vector<Timestamp> is sorted
    # with std::is_sorted AND has no adjacent equal elements.
    if not df.index.is_monotonic_increasing or not df.index.is_unique:
        raise ValueError(
            "DatetimeIndex must be sorted in strictly ascending order "
            "(no duplicates allowed)"
        )

    # ------------------------------------------------------------------
    # Step 6: Symbol argument validation
    # ------------------------------------------------------------------
    # The symbol is optional (default None).  If the caller provides it,
    # it must be a non-empty string of at most 10 characters.
    if symbol is not None:
        # Check type first — must be a string.
        if not isinstance(symbol, str):
            raise ValueError(
                "symbol must be a non-empty string of at most 10 characters, "
                f"but received {type(symbol).__name__}"
            )
        # Check not empty.
        if len(symbol) == 0:
            raise ValueError(
                "symbol must be a non-empty string of at most 10 characters"
            )
        # Check length limit.
        if len(symbol) > 10:
            raise ValueError(
                "symbol must be a non-empty string of at most 10 characters, "
                f"but received a string of length {len(symbol)}"
            )

    # ------------------------------------------------------------------
    # Step 7: Insufficient data — early return with HOLD
    # ------------------------------------------------------------------
    # To compute crossover, we need SMAs for both the current bar AND the
    # previous bar.  The long SMA for the previous bar requires
    # long_window prices ending one bar before the last, so we need at
    # least long_window + 1 total rows.
    if len(df) < config.long_window + 1:
        # Build the early-return dictionary.
        result = {
            "signal": "HOLD",
            "short_sma": None,
            "long_sma": None,
            "timestamp": df.index[-1],  # Last row's timestamp (UTC-aware)
        }
        # Include symbol in the dict if a valid symbol was provided.
        if symbol is not None:
            result["symbol"] = symbol
        return result

    # ------------------------------------------------------------------
    # Step 8: SMA computation
    # ------------------------------------------------------------------
    # We compute four SMA values:
    #   - short_sma_current: mean of last short_window close prices
    #   - long_sma_current:  mean of last long_window close prices
    #   - short_sma_prev:    mean of short_window close prices ending
    #                        one bar before the current (last) bar
    #   - long_sma_prev:     mean of long_window close prices ending
    #                        one bar before the current (last) bar
    #
    # We use explicit sum/count rather than .mean() to have full control
    # over the arithmetic (Requirement 2.4: no additional rounding).
    #
    # .iloc[] is pandas' integer-location indexer — like array subscript
    # in C++.  Negative indices count from the end:
    #   iloc[-N:]   → the last N elements (slice from -N to end)
    #   iloc[-N-1:-1] → N elements ending one before the last

    close = df["close"]  # Extract the close column as a pandas Series

    # --- Current bar SMAs ---
    # Last short_window values:
    short_sma_current = (
        close.iloc[-config.short_window:].sum() / config.short_window
    )
    # Last long_window values:
    long_sma_current = (
        close.iloc[-config.long_window:].sum() / config.long_window
    )

    # --- Previous bar SMAs ---
    # short_window values ending one bar before the last:
    short_sma_prev = (
        close.iloc[-(config.short_window + 1):-1].sum() / config.short_window
    )
    # long_window values ending one bar before the last:
    long_sma_prev = (
        close.iloc[-(config.long_window + 1):-1].sum() / config.long_window
    )

    # ------------------------------------------------------------------
    # Step 9: Crossover detection
    # ------------------------------------------------------------------
    # A "crossover" means the relative position of short vs long SMA
    # CHANGED between the previous bar and the current bar.
    #
    # BUY signal:  short crossed ABOVE long
    #   current: short > long   (short is now above)
    #   previous: short <= long (short was at or below)
    #
    # SELL signal: short crossed BELOW long
    #   current: short < long   (short is now below)
    #   previous: short >= long (short was at or above)
    #
    # HOLD: no crossover occurred (short stayed on same side, or equal)

    if short_sma_current > long_sma_current and short_sma_prev <= long_sma_prev:
        signal = "BUY"
    elif short_sma_current < long_sma_current and short_sma_prev >= long_sma_prev:
        signal = "SELL"
    else:
        signal = "HOLD"

    # ------------------------------------------------------------------
    # Step 10: Build and return the result dictionary
    # ------------------------------------------------------------------
    result = {
        "signal": signal,
        "short_sma": float(short_sma_current),  # Ensure native Python float
        "long_sma": float(long_sma_current),     # Ensure native Python float
        "timestamp": df.index[-1],               # UTC-aware pandas Timestamp
    }

    # Include symbol only if a valid symbol was provided (Requirement 6.3/6.4).
    if symbol is not None:
        result["symbol"] = symbol

    return result
