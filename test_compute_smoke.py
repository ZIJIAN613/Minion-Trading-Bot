"""Smoke test for strategy/compute.py — verifies all validation and computation paths."""

import pandas as pd
import numpy as np
from strategy.config_loader import StrategyConfig
from strategy.compute import compute_signal


def make_df(close_prices, start="2024-01-01", freq="h"):
    """Helper: build a valid bar DataFrame from a list of close prices."""
    n = len(close_prices)
    dates = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    df = pd.DataFrame({
        "open": [100.0] * n,
        "high": [200.0] * n,
        "low": [50.0] * n,
        "close": close_prices,
        "volume": [1000] * n,
    }, index=dates)
    df["volume"] = df["volume"].astype("int64")
    return df


def test_type_error():
    try:
        compute_signal("not a dataframe", StrategyConfig(10, 30))
        assert False, "should have raised TypeError"
    except TypeError as e:
        assert "str" in str(e)
    print("PASS: TypeError")


def test_empty_dataframe():
    dates = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]")
    empty_df = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []}, index=dates
    )
    empty_df = empty_df.astype(
        {"open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "int64"}
    )
    try:
        compute_signal(empty_df, StrategyConfig(10, 30))
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "at least one row" in str(e)
    print("PASS: Empty DataFrame")


def test_insufficient_data():
    df = make_df([100.0] * 5)
    result = compute_signal(df, StrategyConfig(2, 5))
    assert result["signal"] == "HOLD"
    assert result["short_sma"] is None
    assert result["long_sma"] is None
    assert result["timestamp"] == df.index[-1]
    assert "symbol" not in result
    print("PASS: Insufficient data")


def test_insufficient_data_with_symbol():
    df = make_df([100.0] * 5)
    result = compute_signal(df, StrategyConfig(2, 5), symbol="AAPL")
    assert result["signal"] == "HOLD"
    assert result["short_sma"] is None
    assert result["symbol"] == "AAPL"
    print("PASS: Insufficient data with symbol")


def test_buy_signal():
    # close = [100, 100, 100, 200], config short=2, long=3
    # prev: short = (100+100)/2=100, long = (100+100+100)/3=100 => short<=long
    # curr: short = (100+200)/2=150, long = (100+100+200)/3=133.33 => short>long
    # => BUY
    df = make_df([100.0, 100.0, 100.0, 200.0])
    result = compute_signal(df, StrategyConfig(2, 3))
    assert result["signal"] == "BUY"
    print("PASS: BUY signal")


def test_sell_signal():
    # close = [100, 100, 100, 50], config short=2, long=3
    # prev: short = (100+100)/2=100, long = (100+100+100)/3=100 => short>=long
    # curr: short = (100+50)/2=75, long = (100+100+50)/3=83.33 => short<long
    # => SELL
    df = make_df([100.0, 100.0, 100.0, 50.0])
    result = compute_signal(df, StrategyConfig(2, 3))
    assert result["signal"] == "SELL"
    print("PASS: SELL signal")


def test_hold_signal():
    # close = [100, 100, 100, 100], config short=2, long=3
    # prev: short=100, long=100 => equal
    # curr: short=100, long=100 => equal
    # => HOLD (no crossover)
    df = make_df([100.0, 100.0, 100.0, 100.0])
    result = compute_signal(df, StrategyConfig(2, 3))
    assert result["signal"] == "HOLD"
    print("PASS: HOLD signal")


def test_nan_check():
    df = make_df([100.0, float("nan"), 100.0, 200.0])
    try:
        compute_signal(df, StrategyConfig(2, 3))
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "1" in str(e)  # 1 NaN value
    print("PASS: NaN check")


def test_unsorted_index():
    dates = pd.DatetimeIndex(
        ["2024-01-01 03:00", "2024-01-01 01:00", "2024-01-01 02:00", "2024-01-01 04:00"],
        tz="UTC",
    )
    df = pd.DataFrame({
        "open": [100.0] * 4, "high": [200.0] * 4, "low": [50.0] * 4,
        "close": [100.0] * 4, "volume": [1000] * 4,
    }, index=dates)
    df["volume"] = df["volume"].astype("int64")
    try:
        compute_signal(df, StrategyConfig(2, 3))
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "ascending" in str(e)
    print("PASS: Unsorted index")


def test_duplicate_timestamps():
    dates = pd.DatetimeIndex(
        ["2024-01-01 01:00", "2024-01-01 01:00", "2024-01-01 02:00", "2024-01-01 03:00"],
        tz="UTC",
    )
    df = pd.DataFrame({
        "open": [100.0] * 4, "high": [200.0] * 4, "low": [50.0] * 4,
        "close": [100.0] * 4, "volume": [1000] * 4,
    }, index=dates)
    df["volume"] = df["volume"].astype("int64")
    try:
        compute_signal(df, StrategyConfig(2, 3))
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "ascending" in str(e) or "duplicate" in str(e).lower()
    print("PASS: Duplicate timestamps")


def test_invalid_symbols():
    df = make_df([100.0, 100.0, 100.0, 200.0])
    # Empty string
    try:
        compute_signal(df, StrategyConfig(2, 3), symbol="")
        assert False
    except ValueError:
        pass
    # Too long
    try:
        compute_signal(df, StrategyConfig(2, 3), symbol="TOOLONGSYMBOL")
        assert False
    except ValueError:
        pass
    # Not a string
    try:
        compute_signal(df, StrategyConfig(2, 3), symbol=123)
        assert False
    except ValueError:
        pass
    print("PASS: Invalid symbols")


def test_symbol_in_output():
    df = make_df([100.0, 100.0, 100.0, 200.0])
    result = compute_signal(df, StrategyConfig(2, 3), symbol="AAPL")
    assert result["symbol"] == "AAPL"
    print("PASS: Symbol in output")


def test_no_symbol_key_absent():
    df = make_df([100.0, 100.0, 100.0, 200.0])
    result = compute_signal(df, StrategyConfig(2, 3))
    assert "symbol" not in result
    print("PASS: No symbol -> key absent")


def test_timestamp_utc_aware():
    df = make_df([100.0, 100.0, 100.0, 200.0])
    result = compute_signal(df, StrategyConfig(2, 3))
    assert result["timestamp"] == df.index[-1]
    assert result["timestamp"].tzinfo is not None
    print("PASS: Timestamp UTC-aware")


def test_sma_accuracy():
    # Manually verify SMA values
    close = [10.0, 20.0, 30.0, 40.0, 50.0]
    df = make_df(close)
    config = StrategyConfig(2, 4)
    # Need long_window + 1 = 5 rows, we have exactly 5 — sufficient!
    result = compute_signal(df, config)
    # short_sma_current = (40+50)/2 = 45
    # long_sma_current = (20+30+40+50)/4 = 35
    assert abs(result["short_sma"] - 45.0) < 1e-9
    assert abs(result["long_sma"] - 35.0) < 1e-9
    print("PASS: SMA accuracy")


if __name__ == "__main__":
    test_type_error()
    test_empty_dataframe()
    test_insufficient_data()
    test_insufficient_data_with_symbol()
    test_buy_signal()
    test_sell_signal()
    test_hold_signal()
    test_nan_check()
    test_unsorted_index()
    test_duplicate_timestamps()
    test_invalid_symbols()
    test_symbol_in_output()
    test_no_symbol_key_absent()
    test_timestamp_utc_aware()
    test_sma_accuracy()
    print("\nALL TESTS PASSED!")
