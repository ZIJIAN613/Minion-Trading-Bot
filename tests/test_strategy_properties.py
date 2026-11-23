"""
tests/test_strategy_properties.py — Property-based tests for the strategy module.

Eleven property groups, all driven by the Hypothesis library.

Hypothesis overview for C++ developers:
  - Think of @given as a parameterised test (like TEST_P in Google Test) that
    automatically generates hundreds of random inputs according to "strategies".
  - A "strategy" (st.integers, st.text, etc.) is a generator of typed random
    values — analogous to a fuzzer input generator.
  - @settings(max_examples=N) controls how many random examples are tried.
  - assume(condition) discards the current example when the condition is False,
    acting like a filter that narrows the input space without changing the
    generator itself.

Properties tested here:
  Property 1  — SMA computation matches independent calculation (task 3.2)
  Property 2  — Signal matches crossover condition (task 3.3)
  Property 3  — Output structure invariant (task 3.4)
  Property 4  — Non-DataFrame input raises TypeError with type name (task 5.1)
  Property 5  — NaN in close column raises ValueError with count (task 5.2)
  Property 6  — Unsorted index raises ValueError (task 5.3)
  Property 7  — Insufficient data returns HOLD with None SMAs (task 5.4)
  Property 8  — Determinism — identical inputs produce identical outputs (task 5.5)
  Property 9  — Invalid config values always raise ValueError (task 1.2)
  Property 10 — Invalid symbol raises ValueError (task 5.6)
  Property 11 — Empty DataFrame raises ValueError (task 5.7)

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5,
           3.1, 3.2, 3.3, 3.4, 3.5, 4.5, 4.6, 4.7, 6.1, 6.2, 6.3, 6.4,
           6.5, 7.1, 7.3

Run with:  venv\\Scripts\\python.exe -m pytest tests/test_strategy_properties.py -v
"""

import math        # math.isclose — safe floating-point comparison
import os          # os.path — file path utilities
import tempfile    # tempfile — for creating temp config files

import numpy as np     # numpy — pandas' underlying numerical engine
import pandas as pd    # pandas — DataFrame manipulation library
import pytest          # pytest — test runner and assertion helpers
import yaml            # PyYAML — YAML serialization for config testing

# @given — the core Hypothesis decorator that turns a normal test into a
# property-based test by generating random inputs automatically.
from hypothesis import assume, given, settings

# strategies (aliased as st) — the library of input generators.
# st.integers(1, 10) produces random ints in [1, 10], st.text() produces
# random strings, st.sampled_from(seq) picks uniformly from a sequence, etc.
from hypothesis import strategies as st

# Import the modules under test directly (as specified in requirements).
from strategy.config_loader import StrategyConfig, load_config
from strategy.compute import compute_signal


# ===========================================================================
# Generator Strategies
# ===========================================================================
# These are reusable Hypothesis "strategies" — random input generators that
# produce values conforming to specific shapes.  Think of them as typed
# random-value factories analogous to C++ test fixture generators.
# ===========================================================================


def valid_bar_dataframe(min_rows: int = 1, max_rows: int = 200):
    """
    Hypothesis strategy that generates DataFrames conforming to the Bar schema.

    Produces a DataFrame with:
      - UTC-aware DatetimeIndex (strictly ascending, unique timestamps)
      - Columns: open, high, low, close (float64), volume (int64)
      - Random close prices in a reasonable range [1.0, 1000.0]
      - open/high/low derived from close to maintain realistic OHLC relationships

    Args:
        min_rows: Minimum number of rows in the generated DataFrame.
        max_rows: Maximum number of rows in the generated DataFrame.

    Returns:
        A Hypothesis strategy that produces valid Bar DataFrames.
    """
    # @st.composite is a DECORATOR that lets us build complex strategies by
    # combining simpler ones.  The 'draw' parameter is provided by Hypothesis
    # and lets us "draw" (generate) values from other strategies inside the
    # function body.  This is analogous to building a C++ test fixture that
    # calls multiple random generators internally.
    @st.composite
    def _strategy(draw):
        # Draw the number of rows from the specified range.
        n_rows = draw(st.integers(min_value=min_rows, max_value=max_rows))

        # Generate n_rows random close prices in [1.0, 1000.0].
        # st.lists generates a Python list of the given strategy's outputs.
        close_prices = draw(
            st.lists(
                st.floats(min_value=1.0, max_value=1000.0, allow_nan=False,
                          allow_infinity=False),
                min_size=n_rows,
                max_size=n_rows,
            )
        )

        # Build OHLC from close: open = close * 0.99, high = close * 1.01,
        # low = close * 0.98.  This keeps values realistic without needing
        # complex cross-column constraints.
        opens = [c * 0.99 for c in close_prices]
        highs = [c * 1.01 for c in close_prices]
        lows = [c * 0.98 for c in close_prices]
        volumes = [1000] * n_rows  # Simple constant volume

        # Build a UTC-aware DatetimeIndex with unique, ascending timestamps.
        # pd.date_range creates evenly-spaced timestamps; freq="h" means
        # one per hour, guaranteeing strictly ascending order.
        index = pd.date_range(
            start="2020-01-01", periods=n_rows, freq="h", tz="UTC"
        )

        # Construct the DataFrame from the generated data.
        df = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": close_prices,
                "volume": volumes,
            },
            index=index,
        )

        # Cast to exact required dtypes — .astype(dict) applies per-column.
        df = df.astype({
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "int64",
        })

        return df

    return _strategy()


def valid_strategy_config():
    """
    Hypothesis strategy that generates valid StrategyConfig instances.

    Generates short_window in [1, 199] and long_window in [short_window+1, 200],
    ensuring short_window < long_window and both are in [1, 200] (capped for
    test performance).

    Returns:
        A Hypothesis strategy producing StrategyConfig dataclass instances.
    """
    @st.composite
    def _strategy(draw):
        # Draw short_window first, then long_window must be strictly greater.
        short = draw(st.integers(min_value=1, max_value=199))
        long = draw(st.integers(min_value=short + 1, max_value=200))
        return StrategyConfig(short_window=short, long_window=long)

    return _strategy()


def invalid_df_values():
    """
    Hypothesis strategy that generates values that are NOT pandas DataFrames.

    Produces None, integers, strings, lists, dicts, floats, and tuples.

    Returns:
        A Hypothesis strategy producing non-DataFrame values.
    """
    # st.one_of — picks from one of several alternative strategies at random.
    # This is like a union type in C++ — each run picks a different variant.
    return st.one_of(
        st.none(),                                       # None
        st.integers(min_value=-1000, max_value=1000),    # int
        st.text(min_size=0, max_size=20),                # str
        st.lists(st.integers(), min_size=0, max_size=5), # list
        st.dictionaries(                                  # dict
            keys=st.text(min_size=1, max_size=5),
            values=st.integers(),
            min_size=0,
            max_size=3,
        ),
        st.floats(allow_nan=False, allow_infinity=False), # float
        st.tuples(st.integers(), st.integers()),          # tuple
    )


def valid_symbol():
    """
    Hypothesis strategy that generates valid symbol strings.

    Produces non-empty strings of 1–10 uppercase characters (simulating
    ticker symbols like "AAPL", "GOOG", etc.).

    Returns:
        A Hypothesis strategy producing valid symbol strings.
    """
    return st.text(
        # whitelist_categories=("Lu",) restricts to Unicode uppercase letters.
        alphabet=st.characters(whitelist_categories=("Lu",)),
        min_size=1,
        max_size=10,
    )


def invalid_symbol():
    """
    Hypothesis strategy that generates INVALID symbol values.

    Produces empty strings, integers, or strings longer than 10 chars.

    NOTE: None is NOT included here because None means "don't provide a symbol"
    in compute_signal's API — it's a valid way to omit the symbol, not an
    invalid symbol value.  The code only validates symbol when it is not None.

    Returns:
        A Hypothesis strategy producing invalid symbol values.
    """
    return st.one_of(
        st.just(""),                                      # empty string
        st.integers(min_value=-100, max_value=100),       # integer (not str)
        # String longer than 10 characters:
        st.text(
            alphabet=st.characters(whitelist_categories=("Lu",)),
            min_size=11,
            max_size=20,
        ),
    )


# ===========================================================================
# Property 1 — SMA computation matches independent calculation
# ===========================================================================
# Feature: strategy-engine, Property 1: SMA computation matches independent calculation
# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5


class TestProperty1SMAComputation:
    """
    Property 1: For any valid Bar DataFrame with at least long_window + 1 rows
    and any valid configuration, the short_sma and long_sma values in the
    returned dictionary SHALL equal the independently computed arithmetic mean
    (sum of last N close prices divided by N) within a tolerance of 1e-9.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_sma_matches_independent_calculation(self, data) -> None:
        """
        **Property 1** — Generate a valid DataFrame with sufficient rows,
        compute SMAs independently (sum of last N / N), and compare with
        the returned short_sma and long_sma within 1e-9 tolerance.

        Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
        """
        # Draw a valid config first so we know the window sizes.
        # Cap long_window at 150 so that min_rows (long_window+1) never
        # exceeds our 200-row cap for the DataFrame generator.
        config = data.draw(valid_strategy_config())
        assume(config.long_window <= 150)

        # Need at least long_window + 1 rows to compute SMAs.
        min_rows = config.long_window + 1
        # Cap at 200 for performance — guaranteed min_rows <= 151 <= 200.
        max_rows = max(min_rows + 20, min_rows)

        df = data.draw(valid_bar_dataframe(min_rows=min_rows, max_rows=max_rows))

        # Call the function under test.
        result = compute_signal(df, config)

        # --- Independent SMA calculation ---
        # Extract the close prices as a Python list for clarity.
        close = df["close"]

        # Short SMA for the current (last) bar: mean of last short_window prices.
        expected_short_sma = close.iloc[-config.short_window:].sum() / config.short_window

        # Long SMA for the current (last) bar: mean of last long_window prices.
        expected_long_sma = close.iloc[-config.long_window:].sum() / config.long_window

        # Compare with tolerance of 1e-9.
        # math.isclose checks relative and absolute tolerance — Python's
        # equivalent of comparing floats with an epsilon in C++.
        assert math.isclose(result["short_sma"], expected_short_sma, abs_tol=1e-9), (
            f"short_sma mismatch: got {result['short_sma']}, "
            f"expected {expected_short_sma}"
        )
        assert math.isclose(result["long_sma"], expected_long_sma, abs_tol=1e-9), (
            f"long_sma mismatch: got {result['long_sma']}, "
            f"expected {expected_long_sma}"
        )


# ===========================================================================
# Property 2 — Signal matches crossover condition
# ===========================================================================
# Feature: strategy-engine, Property 2: Signal matches crossover condition
# Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5


class TestProperty2SignalCrossover:
    """
    Property 2: For any valid Bar DataFrame with at least long_window + 1 rows,
    the returned signal SHALL be BUY iff short_current > long_current AND
    short_prev <= long_prev; SELL iff short_current < long_current AND
    short_prev >= long_prev; and HOLD otherwise.

    Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_signal_matches_crossover_rules(self, data) -> None:
        """
        **Property 2** — Generate a valid DataFrame with sufficient rows,
        independently compute current/previous SMAs, verify returned signal
        matches BUY/SELL/HOLD crossover rules exactly.

        Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
        """
        config = data.draw(valid_strategy_config())
        # Cap long_window so min_rows never exceeds max_rows cap.
        assume(config.long_window <= 150)

        # Need at least long_window + 1 rows.
        min_rows = config.long_window + 1
        max_rows = max(min_rows + 20, min_rows)

        df = data.draw(valid_bar_dataframe(min_rows=min_rows, max_rows=max_rows))

        result = compute_signal(df, config)

        # --- Independent crossover computation ---
        close = df["close"]

        # Current bar SMAs (last N prices).
        short_sma_current = close.iloc[-config.short_window:].sum() / config.short_window
        long_sma_current = close.iloc[-config.long_window:].sum() / config.long_window

        # Previous bar SMAs (N prices ending one bar before last).
        short_sma_prev = (
            close.iloc[-(config.short_window + 1):-1].sum() / config.short_window
        )
        long_sma_prev = (
            close.iloc[-(config.long_window + 1):-1].sum() / config.long_window
        )

        # Determine expected signal using crossover rules.
        if short_sma_current > long_sma_current and short_sma_prev <= long_sma_prev:
            expected_signal = "BUY"
        elif short_sma_current < long_sma_current and short_sma_prev >= long_sma_prev:
            expected_signal = "SELL"
        else:
            expected_signal = "HOLD"

        assert result["signal"] == expected_signal, (
            f"Signal mismatch: got '{result['signal']}', expected '{expected_signal}'. "
            f"short_curr={short_sma_current}, long_curr={long_sma_current}, "
            f"short_prev={short_sma_prev}, long_prev={long_sma_prev}"
        )


# ===========================================================================
# Property 3 — Output structure invariant
# ===========================================================================
# Feature: strategy-engine, Property 3: Output structure invariant
# Validates: Requirements 6.1, 6.2, 6.3, 6.4


class TestProperty3OutputStructure:
    """
    Property 3: For any valid invocation of compute_signal that does not raise,
    the returned dictionary SHALL contain exactly the keys {signal, short_sma,
    long_sma, timestamp} (plus symbol iff provided); timestamp SHALL equal
    df.index[-1] and be UTC-aware; and signal SHALL be one of BUY/SELL/HOLD.

    Validates: Requirements 6.1, 6.2, 6.3, 6.4
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_output_structure_without_symbol(self, data) -> None:
        """
        **Property 3a** — Without symbol argument, result has exactly 4 keys:
        signal, short_sma, long_sma, timestamp.

        Validates: Requirements 6.1, 6.2, 6.3, 6.4
        """
        config = data.draw(valid_strategy_config())

        # Generate a DataFrame with at least 1 row (may or may not have
        # sufficient rows for SMA — tests both code paths).
        df = data.draw(valid_bar_dataframe(min_rows=1, max_rows=200))

        result = compute_signal(df, config)

        # Keys must be exactly these four (no symbol key).
        expected_keys = {"signal", "short_sma", "long_sma", "timestamp"}
        assert set(result.keys()) == expected_keys, (
            f"Expected keys {expected_keys}, got {set(result.keys())}"
        )

        # Signal must be one of the three valid literals.
        assert result["signal"] in ("BUY", "SELL", "HOLD"), (
            f"Signal must be BUY/SELL/HOLD, got '{result['signal']}'"
        )

        # Timestamp must equal df.index[-1].
        assert result["timestamp"] == df.index[-1], (
            f"Timestamp mismatch: got {result['timestamp']}, "
            f"expected {df.index[-1]}"
        )

        # Timestamp must be UTC-aware (tz is not None).
        # .tzinfo is the timezone attribute on pandas Timestamps.
        assert result["timestamp"].tzinfo is not None, (
            "Timestamp must be UTC-aware (tzinfo should not be None)"
        )

    @given(data=st.data())
    @settings(max_examples=100)
    def test_output_structure_with_symbol(self, data) -> None:
        """
        **Property 3b** — With a valid symbol argument, result has exactly 5
        keys: signal, short_sma, long_sma, timestamp, symbol.

        Validates: Requirements 6.1, 6.2, 6.3, 6.4
        """
        config = data.draw(valid_strategy_config())
        df = data.draw(valid_bar_dataframe(min_rows=1, max_rows=200))
        sym = data.draw(valid_symbol())

        result = compute_signal(df, config, symbol=sym)

        # Keys must include symbol.
        expected_keys = {"signal", "short_sma", "long_sma", "timestamp", "symbol"}
        assert set(result.keys()) == expected_keys, (
            f"Expected keys {expected_keys}, got {set(result.keys())}"
        )

        # Symbol value must match what was passed in.
        assert result["symbol"] == sym, (
            f"Symbol mismatch: got '{result['symbol']}', expected '{sym}'"
        )

        # Signal must still be valid.
        assert result["signal"] in ("BUY", "SELL", "HOLD"), (
            f"Signal must be BUY/SELL/HOLD, got '{result['signal']}'"
        )

        # Timestamp checks.
        assert result["timestamp"] == df.index[-1]
        assert result["timestamp"].tzinfo is not None


# ===========================================================================
# Property 4 — Non-DataFrame input raises TypeError with type name
# ===========================================================================
# Feature: strategy-engine, Property 4: Non-DataFrame input raises TypeError with type name
# Validates: Requirements 1.1


class TestProperty4NonDataFrameTypeError:
    """
    Property 4: For any value that is not a pandas.DataFrame instance
    (including None, integers, strings, lists, dicts), calling compute_signal
    with that value as the df argument SHALL raise a TypeError whose message
    contains the name of the actual type received.

    Validates: Requirements 1.1
    """

    @given(bad_df=invalid_df_values())
    @settings(max_examples=100)
    def test_non_dataframe_raises_type_error(self, bad_df) -> None:
        """
        **Property 4** — Non-DataFrame values must raise TypeError with
        the actual type name in the message.

        Validates: Requirements 1.1
        """
        config = StrategyConfig(short_window=5, long_window=10)

        # pytest.raises is a CONTEXT MANAGER that asserts the named exception
        # is raised inside the `with` block — equivalent to EXPECT_THROW in
        # Google Test.
        with pytest.raises(TypeError) as exc_info:
            compute_signal(bad_df, config)

        # type(bad_df).__name__ gives the human-readable class name.
        # The error message must contain it (e.g. "NoneType", "int", "str").
        actual_type_name = type(bad_df).__name__
        assert actual_type_name in str(exc_info.value), (
            f"Expected type name '{actual_type_name}' in error message, "
            f"got: {exc_info.value}"
        )


# ===========================================================================
# Property 5 — NaN in close column raises ValueError with count
# ===========================================================================
# Feature: strategy-engine, Property 5: NaN in close column raises ValueError with count
# Validates: Requirements 1.3


class TestProperty5NaNInClose:
    """
    Property 5: For any valid Bar DataFrame where one or more close values are
    replaced with NaN, calling compute_signal SHALL raise a ValueError whose
    message contains the exact number of NaN values present in the close column.

    Validates: Requirements 1.3
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_nan_in_close_raises_with_count(self, data) -> None:
        """
        **Property 5** — Inject NaN into close column at random positions,
        verify ValueError message contains exact NaN count.

        Validates: Requirements 1.3
        """
        # Generate a valid DataFrame with at least 2 rows.
        df = data.draw(valid_bar_dataframe(min_rows=2, max_rows=50))
        n_rows = len(df)

        # Choose how many NaN values to inject (at least 1, at most all rows).
        nan_count = data.draw(st.integers(min_value=1, max_value=n_rows))

        # Choose which row indices to corrupt.
        # st.lists with unique_by ensures no duplicates in the index list.
        nan_indices = data.draw(
            st.lists(
                st.integers(min_value=0, max_value=n_rows - 1),
                min_size=nan_count,
                max_size=nan_count,
                unique=True,
            )
        )

        # Inject NaN into the close column at the chosen positions.
        # .iloc[] is pandas' integer-location indexer — like array subscript
        # in C++ (0-based indexing).
        df = df.copy()  # Avoid modifying the original (defensive copy)
        for idx in nan_indices:
            df.iloc[idx, df.columns.get_loc("close")] = np.nan

        config = StrategyConfig(short_window=5, long_window=10)

        with pytest.raises(ValueError) as exc_info:
            compute_signal(df, config)

        # The error message must contain the exact NaN count as a string.
        assert str(nan_count) in str(exc_info.value), (
            f"Expected NaN count '{nan_count}' in error message, "
            f"got: {exc_info.value}"
        )


# ===========================================================================
# Property 6 — Unsorted index raises ValueError
# ===========================================================================
# Feature: strategy-engine, Property 6: Unsorted index raises ValueError
# Validates: Requirements 1.4


class TestProperty6UnsortedIndex:
    """
    Property 6: For any valid Bar DataFrame with 2 or more rows whose
    DatetimeIndex is permuted into non-ascending order, calling compute_signal
    SHALL raise a ValueError whose message indicates the index must be sorted
    in ascending order.

    Validates: Requirements 1.4
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_unsorted_index_raises_value_error(self, data) -> None:
        """
        **Property 6** — Permute index to non-ascending order, verify
        ValueError about ascending order.

        Validates: Requirements 1.4
        """
        # Need at least 2 rows to meaningfully permute the order.
        df = data.draw(valid_bar_dataframe(min_rows=2, max_rows=50))

        # Reverse the DataFrame to guarantee non-ascending order.
        # .iloc[::-1] reverses the rows — like std::reverse on a vector.
        df_reversed = df.iloc[::-1]

        # Verify it's actually unsorted (it must be since we reversed).
        # assume() discards this example if it's somehow still sorted.
        assume(not df_reversed.index.is_monotonic_increasing)

        config = StrategyConfig(short_window=5, long_window=10)

        with pytest.raises(ValueError) as exc_info:
            compute_signal(df_reversed, config)

        # The error message should mention ascending order.
        msg = str(exc_info.value).lower()
        assert "ascending" in msg or "sorted" in msg, (
            f"Expected 'ascending' or 'sorted' in error message, "
            f"got: {exc_info.value}"
        )


# ===========================================================================
# Property 7 — Insufficient data returns HOLD with None SMAs
# ===========================================================================
# Feature: strategy-engine, Property 7: Insufficient data returns HOLD with None SMAs
# Validates: Requirements 1.2, 6.3, 6.4


class TestProperty7InsufficientData:
    """
    Property 7: For any valid Bar DataFrame with fewer than long_window + 1
    rows (but at least 1 row) and any valid configuration, calling compute_signal
    SHALL return a dictionary with signal="HOLD", short_sma=None, long_sma=None,
    timestamp=last row's DatetimeIndex value, and — if a valid symbol was
    provided — symbol set to that string (if no symbol, the key is absent).

    Validates: Requirements 1.2, 6.3, 6.4
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_insufficient_data_returns_hold_no_symbol(self, data) -> None:
        """
        **Property 7a** — Insufficient data without symbol returns HOLD,
        None SMAs, correct timestamp, no symbol key.

        Validates: Requirements 1.2, 6.3, 6.4
        """
        config = data.draw(valid_strategy_config())

        # Generate a DataFrame with 1 to long_window rows (insufficient).
        max_insufficient = config.long_window
        df = data.draw(valid_bar_dataframe(min_rows=1, max_rows=max_insufficient))

        result = compute_signal(df, config)

        assert result["signal"] == "HOLD", (
            f"Expected HOLD for insufficient data, got '{result['signal']}'"
        )
        assert result["short_sma"] is None, (
            f"Expected None short_sma, got {result['short_sma']}"
        )
        assert result["long_sma"] is None, (
            f"Expected None long_sma, got {result['long_sma']}"
        )
        assert result["timestamp"] == df.index[-1], (
            f"Timestamp mismatch: got {result['timestamp']}, expected {df.index[-1]}"
        )
        # No symbol key when not provided.
        assert "symbol" not in result, (
            f"symbol key should be absent when not provided, but got: {result}"
        )

    @given(data=st.data())
    @settings(max_examples=100)
    def test_insufficient_data_returns_hold_with_symbol(self, data) -> None:
        """
        **Property 7b** — Insufficient data with valid symbol returns HOLD,
        None SMAs, correct timestamp, and symbol in the dict.

        Validates: Requirements 1.2, 6.3, 6.4
        """
        config = data.draw(valid_strategy_config())

        max_insufficient = config.long_window
        df = data.draw(valid_bar_dataframe(min_rows=1, max_rows=max_insufficient))
        sym = data.draw(valid_symbol())

        result = compute_signal(df, config, symbol=sym)

        assert result["signal"] == "HOLD"
        assert result["short_sma"] is None
        assert result["long_sma"] is None
        assert result["timestamp"] == df.index[-1]
        # Symbol key present and matches.
        assert result["symbol"] == sym, (
            f"Expected symbol '{sym}', got '{result.get('symbol')}'"
        )


# ===========================================================================
# Property 8 — Determinism — identical inputs produce identical outputs
# ===========================================================================
# Feature: strategy-engine, Property 8: Determinism — identical inputs produce identical outputs
# Validates: Requirements 7.1, 7.3


class TestProperty8Determinism:
    """
    Property 8: For any valid Bar DataFrame, valid configuration, and optional
    symbol argument, calling compute_signal twice with the same arguments SHALL
    produce dictionaries that are exactly equal (same signal, timestamp, symbol)
    with short_sma and long_sma equal within 1e-9 tolerance.

    Validates: Requirements 7.1, 7.3
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_determinism_without_symbol(self, data) -> None:
        """
        **Property 8a** — Two calls with identical args (no symbol) produce
        identical results.

        Validates: Requirements 7.1, 7.3
        """
        config = data.draw(valid_strategy_config())
        df = data.draw(valid_bar_dataframe(min_rows=1, max_rows=200))

        result1 = compute_signal(df, config)
        result2 = compute_signal(df, config)

        # Signal, timestamp must be exactly equal.
        assert result1["signal"] == result2["signal"], (
            f"Signal non-deterministic: '{result1['signal']}' vs '{result2['signal']}'"
        )
        assert result1["timestamp"] == result2["timestamp"], (
            f"Timestamp non-deterministic"
        )

        # SMAs: both None or equal within 1e-9.
        if result1["short_sma"] is None:
            assert result2["short_sma"] is None
        else:
            assert math.isclose(
                result1["short_sma"], result2["short_sma"], abs_tol=1e-9
            )

        if result1["long_sma"] is None:
            assert result2["long_sma"] is None
        else:
            assert math.isclose(
                result1["long_sma"], result2["long_sma"], abs_tol=1e-9
            )

    @given(data=st.data())
    @settings(max_examples=100)
    def test_determinism_with_symbol(self, data) -> None:
        """
        **Property 8b** — Two calls with identical args (with symbol) produce
        identical results including symbol key.

        Validates: Requirements 7.1, 7.3
        """
        config = data.draw(valid_strategy_config())
        df = data.draw(valid_bar_dataframe(min_rows=1, max_rows=200))
        sym = data.draw(valid_symbol())

        result1 = compute_signal(df, config, symbol=sym)
        result2 = compute_signal(df, config, symbol=sym)

        assert result1["signal"] == result2["signal"]
        assert result1["timestamp"] == result2["timestamp"]
        assert result1["symbol"] == result2["symbol"]

        if result1["short_sma"] is None:
            assert result2["short_sma"] is None
        else:
            assert math.isclose(
                result1["short_sma"], result2["short_sma"], abs_tol=1e-9
            )

        if result1["long_sma"] is None:
            assert result2["long_sma"] is None
        else:
            assert math.isclose(
                result1["long_sma"], result2["long_sma"], abs_tol=1e-9
            )


# ===========================================================================
# Property 9 — Invalid config values always raise ValueError
# ===========================================================================
# Feature: strategy-engine, Property 9: Invalid config values always raise ValueError
# Validates: Requirements 4.5, 4.6, 4.7


class TestProperty9InvalidConfig:
    """
    Property 9: For any YAML configuration where short_window or long_window is
    not an integer in [1, 10000], or where short_window >= long_window, calling
    load_config SHALL raise a ValueError whose message identifies the invalid key.

    Validates: Requirements 4.5, 4.6, 4.7
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_out_of_range_values_raise_value_error(self, data) -> None:
        """
        **Property 9a** — Out-of-range integer values raise ValueError
        identifying the invalid key.

        Validates: Requirements 4.5, 4.6
        """
        # Choose which key to make invalid.
        bad_key = data.draw(st.sampled_from(["short_window", "long_window"]))

        # Generate a bad value: integer outside [1, 10000].
        bad_value = data.draw(st.one_of(
            st.integers(max_value=0),        # Too small (0 or negative)
            st.integers(min_value=10001),     # Too large
        ))

        # The other key gets a valid value.
        if bad_key == "short_window":
            config_data = {"short_window": bad_value, "long_window": 50}
        else:
            config_data = {"short_window": 5, "long_window": bad_value}

        # Write to a temporary YAML file.
        # tempfile.NamedTemporaryFile creates a temporary file — Python's
        # equivalent of a C++ RAII temp file handle.  delete=False keeps the
        # file on disk until we manually delete it.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(config_data, f)
            tmp_path = f.name

        try:
            with pytest.raises(ValueError) as exc_info:
                load_config(tmp_path)

            # The error message should identify the problematic key.
            assert bad_key in str(exc_info.value), (
                f"Expected key name '{bad_key}' in error message, "
                f"got: {exc_info.value}"
            )
        finally:
            # Clean up the temp file — defensive cleanup like C++ destructor.
            os.unlink(tmp_path)

    @given(data=st.data())
    @settings(max_examples=100)
    def test_non_integer_values_raise_value_error(self, data) -> None:
        """
        **Property 9b** — Non-integer values (float, string, bool, None)
        raise ValueError identifying the invalid key.

        Validates: Requirements 4.5, 4.6
        """
        bad_key = data.draw(st.sampled_from(["short_window", "long_window"]))

        # Generate a non-integer value.
        bad_value = data.draw(st.one_of(
            st.floats(min_value=1.1, max_value=100.5, allow_nan=False,
                      allow_infinity=False),                   # float
            st.text(min_size=1, max_size=5),                   # string
            st.booleans(),                                     # bool
            st.none(),                                         # None
        ))

        if bad_key == "short_window":
            config_data = {"short_window": bad_value, "long_window": 50}
        else:
            config_data = {"short_window": 5, "long_window": bad_value}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(config_data, f)
            tmp_path = f.name

        try:
            with pytest.raises(ValueError) as exc_info:
                load_config(tmp_path)

            assert bad_key in str(exc_info.value), (
                f"Expected key name '{bad_key}' in error message, "
                f"got: {exc_info.value}"
            )
        finally:
            os.unlink(tmp_path)

    @given(data=st.data())
    @settings(max_examples=100)
    def test_short_gte_long_raises_value_error(self, data) -> None:
        """
        **Property 9c** — short_window >= long_window raises ValueError.

        Validates: Requirements 4.7
        """
        # Generate values where short >= long.
        short_val = data.draw(st.integers(min_value=1, max_value=10000))
        # long_val is in [1, short_val] so short >= long always holds.
        long_val = data.draw(st.integers(min_value=1, max_value=short_val))

        config_data = {"short_window": short_val, "long_window": long_val}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(config_data, f)
            tmp_path = f.name

        try:
            with pytest.raises(ValueError) as exc_info:
                load_config(tmp_path)

            # Message should mention "short_window" (the constraint violation).
            msg = str(exc_info.value).lower()
            assert "short_window" in msg or "long_window" in msg, (
                f"Expected 'short_window' or 'long_window' in error, "
                f"got: {exc_info.value}"
            )
        finally:
            os.unlink(tmp_path)


# ===========================================================================
# Property 10 — Invalid symbol raises ValueError
# ===========================================================================
# Feature: strategy-engine, Property 10: Invalid symbol raises ValueError
# Validates: Requirements 6.5


class TestProperty10InvalidSymbol:
    """
    Property 10: For any value passed as the symbol argument that is not a
    string, or is an empty string, or exceeds 10 characters, calling
    compute_signal SHALL raise a ValueError whose message states that symbol
    must be a non-empty string.

    Validates: Requirements 6.5
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_invalid_symbol_raises_value_error(self, data) -> None:
        """
        **Property 10** — Invalid symbol values (empty, None, int, >10 chars)
        raise ValueError about symbol constraints.

        Validates: Requirements 6.5
        """
        # Generate a valid DataFrame (at least 1 row).
        df = data.draw(valid_bar_dataframe(min_rows=1, max_rows=50))
        config = StrategyConfig(short_window=5, long_window=10)

        # Draw an invalid symbol value.
        bad_sym = data.draw(invalid_symbol())

        with pytest.raises(ValueError) as exc_info:
            compute_signal(df, config, symbol=bad_sym)

        # The error message should mention "symbol".
        assert "symbol" in str(exc_info.value).lower(), (
            f"Expected 'symbol' in error message, got: {exc_info.value}"
        )


# ===========================================================================
# Property 11 — Empty DataFrame raises ValueError
# ===========================================================================
# Feature: strategy-engine, Property 11: Empty DataFrame raises ValueError
# Validates: Requirements 1.5


class TestProperty11EmptyDataFrame:
    """
    Property 11: For any valid Bar DataFrame that passes schema validation but
    contains zero rows, calling compute_signal SHALL raise a ValueError whose
    message states that the DataFrame must contain at least one row.

    Validates: Requirements 1.5
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_empty_dataframe_raises_value_error(self, data) -> None:
        """
        **Property 11** — A valid-schema DataFrame with zero rows raises
        ValueError about needing at least one row.

        Validates: Requirements 1.5
        """
        # Build an empty DataFrame with the correct schema.
        # pd.DatetimeIndex([], dtype="datetime64[ns, UTC]") creates an empty
        # UTC-aware DatetimeIndex — equivalent to an empty C++ vector<Timestamp>.
        index = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]")

        df = pd.DataFrame(
            {
                "open": pd.Series([], dtype="float64"),
                "high": pd.Series([], dtype="float64"),
                "low": pd.Series([], dtype="float64"),
                "close": pd.Series([], dtype="float64"),
                "volume": pd.Series([], dtype="int64"),
            },
            index=index,
        )

        config = data.draw(valid_strategy_config())

        with pytest.raises(ValueError) as exc_info:
            compute_signal(df, config)

        # The error message should mention "at least one row".
        msg = str(exc_info.value).lower()
        assert "at least one row" in msg or "row" in msg, (
            f"Expected message about needing at least one row, "
            f"got: {exc_info.value}"
        )
