# tests/test_strategy_unit.py
#
# Example-based unit tests for the strategy module using pytest.
#
# These tests cover:
#   - Config loading: missing file, invalid YAML syntax, default values
#   - Signal generation: BUY, SELL, HOLD via hand-crafted crossover scenarios
#
# Validates: Requirements 3.1, 3.2, 3.3, 4.1, 4.3, 4.4

import pytest                       # pytest — the standard Python test framework
import pandas as pd                 # pandas — DataFrame library
import numpy as np                  # numpy — numerical operations

from strategy.config_loader import StrategyConfig, load_config
from strategy.compute import compute_signal


# ---------------------------------------------------------------------------
# Helper function
# ---------------------------------------------------------------------------

def make_df(close_prices, start="2024-01-01", freq="h"):
    """
    Build a valid bar DataFrame from a list of close prices.

    This helper constructs the exact schema that compute_signal() expects:
    a UTC-aware DatetimeIndex with OHLCV columns.  The 'open', 'high', 'low'
    columns are set to fixed dummy values since only 'close' affects SMA logic.

    Args:
        close_prices: A Python list of float values for the 'close' column.
        start:        Start date for the generated DatetimeIndex.
        freq:         Frequency string for time spacing (default: hourly).

    Returns:
        A pandas DataFrame matching the Bar_DataFrame schema.
    """
    n = len(close_prices)
    # pd.date_range() generates a sequence of evenly spaced timestamps.
    # tz="UTC" makes them timezone-aware — required by compute_signal().
    dates = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [200.0] * n,
            "low": [50.0] * n,
            "close": close_prices,
            "volume": [1000] * n,
        },
        index=dates,
    )
    # Ensure volume is int64 to match the canonical Bar_DataFrame schema.
    df["volume"] = df["volume"].astype("int64")
    return df


# ---------------------------------------------------------------------------
# Configuration tests (Requirements 4.3, 4.4, 4.1)
# ---------------------------------------------------------------------------

def test_config_file_not_found():
    """
    Requirement 4.3: If the config file is absent, raise FileNotFoundError
    with a message identifying the expected file path.

    Validates: Requirements 4.3
    """
    # pytest.raises() is a CONTEXT MANAGER — Python's equivalent of a
    # try/catch block that also asserts the exception type.  The 'match'
    # parameter checks that the exception message contains the given regex.
    with pytest.raises(FileNotFoundError, match="no_such_file.yaml"):
        load_config("no_such_file.yaml")


def test_config_invalid_yaml_syntax(tmp_path):
    """
    Requirement 4.4: If the config file contains invalid YAML syntax,
    raise ValueError describing the syntax error.

    tmp_path is a pytest built-in FIXTURE — it provides a unique temporary
    directory for each test function.  Similar to creating a temp dir in C++
    via std::filesystem::temp_directory_path(), but pytest cleans it up
    automatically after the test session.

    Validates: Requirements 4.4
    """
    # Create a temporary file with intentionally malformed YAML.
    # The colon-space after "bad" starts a mapping, but the unmatched bracket
    # produces a YAML syntax error.
    bad_yaml_file = tmp_path / "bad_config.yaml"
    bad_yaml_file.write_text("short_window: [invalid\n")

    with pytest.raises(ValueError, match="Invalid YAML"):
        load_config(str(bad_yaml_file))


def test_config_loads_default_values():
    """
    Requirement 4.1: The strategy module reads config.yaml from strategy/
    directory.  The default file ships with short_window=10, long_window=30.

    Validates: Requirements 4.1
    """
    config = load_config("strategy/config.yaml")

    # Verify the loaded config matches the shipped defaults.
    assert config == StrategyConfig(10, 30), (
        f"Expected StrategyConfig(10, 30), got {config}"
    )


# ---------------------------------------------------------------------------
# Signal generation tests (Requirements 3.1, 3.2, 3.3)
# ---------------------------------------------------------------------------

def test_buy_signal_specific_example():
    """
    Requirement 3.1: BUY when short SMA crosses above long SMA.

    Hand-crafted scenario with config short=2, long=3 and 4 bars:
      close = [100, 100, 100, 200]

      Previous bar (bar index 2):
        short_sma_prev = (close[1] + close[2]) / 2 = (100+100)/2 = 100
        long_sma_prev  = (close[0] + close[1] + close[2]) / 3 = 100
        => short <= long  (precondition for BUY)

      Current bar (bar index 3):
        short_sma_curr = (close[2] + close[3]) / 2 = (100+200)/2 = 150
        long_sma_curr  = (close[1] + close[2] + close[3]) / 3 = 133.33
        => short > long  (crossover happened!)

      Result: BUY

    Validates: Requirements 3.1
    """
    df = make_df([100.0, 100.0, 100.0, 200.0])
    config = StrategyConfig(short_window=2, long_window=3)

    result = compute_signal(df, config)

    assert result["signal"] == "BUY"
    # Verify the SMA values are populated (not None) since we have enough data.
    assert result["short_sma"] is not None
    assert result["long_sma"] is not None


def test_sell_signal_specific_example():
    """
    Requirement 3.2: SELL when short SMA crosses below long SMA.

    Hand-crafted scenario with config short=2, long=3 and 4 bars:
      close = [100, 100, 100, 50]

      Previous bar (bar index 2):
        short_sma_prev = (close[1] + close[2]) / 2 = (100+100)/2 = 100
        long_sma_prev  = (close[0] + close[1] + close[2]) / 3 = 100
        => short >= long  (precondition for SELL)

      Current bar (bar index 3):
        short_sma_curr = (close[2] + close[3]) / 2 = (100+50)/2 = 75
        long_sma_curr  = (close[1] + close[2] + close[3]) / 3 = 83.33
        => short < long  (crossover happened!)

      Result: SELL

    Validates: Requirements 3.2
    """
    df = make_df([100.0, 100.0, 100.0, 50.0])
    config = StrategyConfig(short_window=2, long_window=3)

    result = compute_signal(df, config)

    assert result["signal"] == "SELL"
    # Verify the SMA values are populated (not None) since we have enough data.
    assert result["short_sma"] is not None
    assert result["long_sma"] is not None


def test_hold_signal_no_crossover():
    """
    Requirement 3.3: HOLD when no crossover occurs.

    Hand-crafted scenario with config short=2, long=3 and 4 bars:
      close = [100, 100, 100, 100]

      Previous bar (bar index 2):
        short_sma_prev = (100+100)/2 = 100
        long_sma_prev  = (100+100+100)/3 = 100
        => short == long  (no crossover yet)

      Current bar (bar index 3):
        short_sma_curr = (100+100)/2 = 100
        long_sma_curr  = (100+100+100)/3 = 100
        => short == long  (still no crossover)

      Result: HOLD (no relative position change)

    Validates: Requirements 3.3
    """
    df = make_df([100.0, 100.0, 100.0, 100.0])
    config = StrategyConfig(short_window=2, long_window=3)

    result = compute_signal(df, config)

    assert result["signal"] == "HOLD"
    # SMA values should be populated since we have enough bars (4 >= 3+1).
    assert result["short_sma"] is not None
    assert result["long_sma"] is not None


# ---------------------------------------------------------------------------
# Type and validation error tests (Requirements 1.1, 1.3, 1.4, 1.5)
# ---------------------------------------------------------------------------

def test_type_error_non_dataframe():
    """
    Requirement 1.1: If df is not a pandas DataFrame, raise TypeError
    whose message states the actual type received.

    Validates: Requirements 1.1
    """
    # pytest.raises() is a CONTEXT MANAGER — asserts that the code inside
    # raises the specified exception type. 'match' checks the message
    # contains the regex pattern.
    with pytest.raises(TypeError, match="str"):
        compute_signal("not a dataframe", StrategyConfig(10, 30))


def test_empty_dataframe_raises():
    """
    Requirement 1.5: If df passes schema validation but has zero rows,
    raise ValueError stating the DataFrame must contain at least one row.

    Validates: Requirements 1.5
    """
    # Build a zero-row DataFrame that still has correct schema (UTC index,
    # proper column dtypes) — it passes schema validation but fails the
    # empty check.
    dates = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]")
    empty_df = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []},
        index=dates,
    )
    # .astype() converts column dtypes — analogous to static_cast in C++.
    empty_df = empty_df.astype(
        {
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "int64",
        }
    )

    with pytest.raises(ValueError, match="at least one row"):
        compute_signal(empty_df, StrategyConfig(10, 30))


def test_insufficient_data_returns_hold():
    """
    Requirement 1.2: If df has fewer rows than long_window + 1, return
    HOLD with None SMAs and correct timestamp. Symbol key is absent when
    no symbol is provided.

    Validates: Requirements 1.2
    """
    df = make_df([100.0] * 5)
    config = StrategyConfig(short_window=2, long_window=5)

    result = compute_signal(df, config)

    assert result["signal"] == "HOLD"
    assert result["short_sma"] is None
    assert result["long_sma"] is None
    assert result["timestamp"] == df.index[-1]
    # When no symbol is provided, the key must be absent from the dict.
    assert "symbol" not in result


def test_insufficient_data_with_symbol():
    """
    Requirement 1.2 + 6.3: Insufficient data returns HOLD and includes
    the symbol key when a valid symbol is provided.

    Validates: Requirements 1.2, 6.3
    """
    df = make_df([100.0] * 5)
    config = StrategyConfig(short_window=2, long_window=5)

    result = compute_signal(df, config, symbol="AAPL")

    assert result["signal"] == "HOLD"
    assert result["short_sma"] is None
    assert result["symbol"] == "AAPL"


def test_nan_in_close_raises():
    """
    Requirement 1.3: If any value in close is NaN, raise ValueError
    stating the number of NaN values found.

    Validates: Requirements 1.3
    """
    df = make_df([100.0, float("nan"), 100.0, 200.0])

    with pytest.raises(ValueError, match="1"):
        compute_signal(df, StrategyConfig(2, 3))


def test_unsorted_index_raises():
    """
    Requirement 1.4: If DatetimeIndex is not strictly ascending, raise
    ValueError stating the index must be sorted in ascending order.

    Validates: Requirements 1.4
    """
    # Manually construct a DataFrame with out-of-order timestamps.
    dates = pd.DatetimeIndex(
        ["2024-01-01 03:00", "2024-01-01 01:00", "2024-01-01 02:00", "2024-01-01 04:00"],
        tz="UTC",
    )
    df = pd.DataFrame(
        {
            "open": [100.0] * 4,
            "high": [200.0] * 4,
            "low": [50.0] * 4,
            "close": [100.0] * 4,
            "volume": [1000] * 4,
        },
        index=dates,
    )
    df["volume"] = df["volume"].astype("int64")

    with pytest.raises(ValueError, match="ascending"):
        compute_signal(df, StrategyConfig(2, 3))


# ---------------------------------------------------------------------------
# Symbol validation tests (Requirements 6.3, 6.4, 6.5)
# ---------------------------------------------------------------------------

def test_invalid_symbol_empty_string():
    """
    Requirement 6.5: Empty string symbol raises ValueError.

    Validates: Requirements 6.5
    """
    df = make_df([100.0, 100.0, 100.0, 200.0])

    with pytest.raises(ValueError, match="symbol"):
        compute_signal(df, StrategyConfig(2, 3), symbol="")


def test_invalid_symbol_too_long():
    """
    Requirement 6.5: Symbol longer than 10 chars raises ValueError.

    Validates: Requirements 6.5
    """
    df = make_df([100.0, 100.0, 100.0, 200.0])

    with pytest.raises(ValueError, match="symbol"):
        compute_signal(df, StrategyConfig(2, 3), symbol="TOOLONGSYMBOL")


def test_invalid_symbol_not_string():
    """
    Requirement 6.5: Non-string symbol raises ValueError.

    Validates: Requirements 6.5
    """
    df = make_df([100.0, 100.0, 100.0, 200.0])

    with pytest.raises(ValueError, match="symbol"):
        compute_signal(df, StrategyConfig(2, 3), symbol=123)


def test_symbol_included_in_output():
    """
    Requirement 6.3: When a valid symbol is provided, the key appears
    in the returned dictionary with that string as its value.

    Validates: Requirements 6.3
    """
    df = make_df([100.0, 100.0, 100.0, 200.0])

    result = compute_signal(df, StrategyConfig(2, 3), symbol="AAPL")

    assert result["symbol"] == "AAPL"


def test_symbol_key_absent_when_not_provided():
    """
    Requirement 6.4: If no symbol is provided, the key is omitted
    entirely from the returned dictionary.

    Validates: Requirements 6.4
    """
    df = make_df([100.0, 100.0, 100.0, 200.0])

    result = compute_signal(df, StrategyConfig(2, 3))

    assert "symbol" not in result


# ---------------------------------------------------------------------------
# Output structure tests (Requirements 6.1, 6.2, 7.1)
# ---------------------------------------------------------------------------

def test_timestamp_is_utc_aware():
    """
    Requirement 6.2: The returned timestamp is UTC-aware and equals the
    last row of the input DataFrame's DatetimeIndex.

    Validates: Requirements 6.2
    """
    df = make_df([100.0, 100.0, 100.0, 200.0])

    result = compute_signal(df, StrategyConfig(2, 3))

    assert result["timestamp"] == df.index[-1]
    # .tzinfo is the timezone object attached to the Timestamp.
    # None would mean timezone-naive — we require it to be set.
    assert result["timestamp"].tzinfo is not None


def test_sma_accuracy():
    """
    Requirement 2.5: SMA values match an independent sum/N calculation
    within 1e-9 tolerance.

    Hand-crafted scenario:
      close = [10, 20, 30, 40, 50], config short=2, long=4
      Needs long_window+1 = 5 rows — we have exactly 5.

      short_sma_current = (40 + 50) / 2 = 45.0
      long_sma_current  = (20 + 30 + 40 + 50) / 4 = 35.0

    Validates: Requirements 2.5
    """
    close = [10.0, 20.0, 30.0, 40.0, 50.0]
    df = make_df(close)
    config = StrategyConfig(short_window=2, long_window=4)

    result = compute_signal(df, config)

    # abs() gives the absolute value — equivalent to std::abs in C++.
    assert abs(result["short_sma"] - 45.0) < 1e-9
    assert abs(result["long_sma"] - 35.0) < 1e-9
