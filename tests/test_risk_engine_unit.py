# tests/test_risk_engine_unit.py
#
# Example-based unit tests for the risk_engine module using pytest.
#
# These tests cover:
#   - Config loading: missing file, invalid YAML syntax, default values
#   - Signal evaluation: APPROVED BUY, APPROVED SELL, VETOED scenarios
#   - Risk check ordering: multiple vetoes report first-in-order
#
# Validates: Requirements 1.1–1.7, 2.1, 3.3, 3.4, 4.2, 5.4, 6.2, 6.3, 7.3, 7.4, 9.2

import pytest                       # pytest — the standard Python test framework
import pandas as pd                 # pandas — DataFrame / Timestamp library

from risk_engine.config_loader import RiskConfig, load_config
from risk_engine.evaluate import evaluate_signal


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def make_signal(signal_type="BUY", symbol="AAPL"):
    """
    Build a valid Signal_Dict for testing.

    Args:
        signal_type: One of "BUY", "SELL", "HOLD".
        symbol:      Ticker symbol string, or None to omit the key.

    Returns:
        A dictionary matching the Signal_Dict schema expected by
        evaluate_signal().

    Note: pd.Timestamp("2024-06-01", tz="UTC") creates a timezone-aware
    pandas Timestamp — Python's high-precision datetime for time-series.
    """
    result = {
        "signal": signal_type,
        "timestamp": pd.Timestamp("2024-06-01 10:00:00", tz="UTC"),
    }
    # Only include the 'symbol' key if caller provided one.
    # In C++ terms: conditionally inserting into the map.
    if symbol is not None:
        result["symbol"] = symbol
    return result


def make_account(equity=100000.0, cash=10000.0, open_positions=None,
                 daily_realized_pnl=0.0):
    """
    Build a valid Account_Snapshot dict for testing.

    Args:
        equity:              Total account value in USD (positive float).
        cash:                Available cash in USD (non-negative float).
        open_positions:      List of position dicts (symbol + size).
                             Defaults to an empty list if None is passed.
        daily_realized_pnl:  Today's realized profit/loss in USD.

    Returns:
        A dictionary matching the Account_Snapshot schema expected by
        evaluate_signal().
    """
    # 'if x is None' checks identity — like comparing to nullptr in C++.
    # We use this pattern to avoid mutable default arguments (a Python
    # gotcha where default lists are shared across calls).
    if open_positions is None:
        open_positions = []

    return {
        "equity": equity,
        "cash": cash,
        "open_positions": open_positions,
        "daily_realized_pnl": daily_realized_pnl,
    }


def make_config(max_position_percent=5.0, max_daily_loss=500.0,
                max_open_positions=5, stop_loss_percent=2.0):
    """
    Build a RiskConfig dataclass with default test values.

    These defaults match the shipped risk_engine/config.yaml file.

    Args:
        max_position_percent: Max % of equity per trade.
        max_daily_loss:       Max realized loss in USD per day.
        max_open_positions:   Max concurrent open positions.
        stop_loss_percent:    % distance for stop-loss from entry.

    Returns:
        A RiskConfig instance.
    """
    return RiskConfig(
        max_position_percent=max_position_percent,
        max_daily_loss=max_daily_loss,
        max_open_positions=max_open_positions,
        stop_loss_percent=stop_loss_percent,
    )


# ---------------------------------------------------------------------------
# Configuration tests (Requirements 7.3, 7.4)
# ---------------------------------------------------------------------------

def test_config_file_not_found():
    """
    Requirement 7.3: If the config file is absent, raise FileNotFoundError
    with a message identifying the expected file path.

    Validates: Requirements 7.3
    """
    # pytest.raises() is a CONTEXT MANAGER — Python's equivalent of a
    # try/catch block that also asserts the exception type.  The 'match'
    # parameter checks that the exception message contains the given regex.
    with pytest.raises(FileNotFoundError, match="no_such_file.yaml"):
        load_config("no_such_file.yaml")


def test_config_invalid_yaml_syntax(tmp_path):
    """
    Requirement 7.4: If the config file contains invalid YAML syntax,
    raise ValueError describing the syntax error.

    tmp_path is a pytest built-in FIXTURE — it provides a unique temporary
    directory for each test function.  Similar to creating a temp dir in C++
    via std::filesystem::temp_directory_path(), but pytest cleans it up
    automatically after the test session.

    Validates: Requirements 7.4
    """
    # Create a temporary file with intentionally malformed YAML.
    # The unmatched bracket produces a YAML syntax error.
    bad_yaml_file = tmp_path / "bad_config.yaml"
    bad_yaml_file.write_text("max_position_percent: [invalid\n")

    with pytest.raises(ValueError, match="Invalid YAML"):
        load_config(str(bad_yaml_file))


def test_config_loads_default_values():
    """
    Requirement 7.1–7.2: The risk engine reads config.yaml from risk_engine/
    directory.  The default file ships with known values.

    Validates: Requirements 7.1, 7.2
    """
    config = load_config("risk_engine/config.yaml")

    # Verify the loaded config matches the shipped defaults.
    # In Python, @dataclass auto-generates __eq__ so we can compare
    # instances directly — like operator== on a C++ struct.
    assert config == RiskConfig(
        max_position_percent=5.0,
        max_daily_loss=500.0,
        max_open_positions=5,
        stop_loss_percent=2.0,
    ), f"Expected default config values, got {config}"


# ---------------------------------------------------------------------------
# SELL without symbol — VETOED (Requirement 5.4)
# ---------------------------------------------------------------------------

def test_sell_without_symbol_vetoed():
    """
    Requirement 5.5: SELL signal without a 'symbol' key returns VETOED
    because the risk engine cannot verify the position is held.

    Validates: Requirements 5.4
    """
    # Build a SELL signal with symbol=None → key is omitted from dict.
    signal = make_signal(signal_type="SELL", symbol=None)
    account = make_account(
        equity=100000.0,
        cash=10000.0,
        open_positions=[{"symbol": "AAPL", "size": 50}],
        daily_realized_pnl=0.0,
    )
    config = make_config()

    result = evaluate_signal(signal, 150.0, account, config)

    assert result["decision"] == "VETOED"
    assert result["signal"] == "SELL"
    assert result["position_size"] == 0
    assert result["stop_loss"] is None
    # The reason should mention that a symbol is required.
    assert "symbol" in result["reason"].lower() or "symbol" in result["reason"]


# ---------------------------------------------------------------------------
# APPROVED BUY — specific example (Requirements 3.3, 6.2)
# ---------------------------------------------------------------------------

def test_approved_buy_specific_example():
    """
    Hand-crafted scenario producing an APPROVED BUY with computed
    position_size and stop_loss.

    Config: max_position_percent=5.0, max_daily_loss=500.0,
            max_open_positions=5, stop_loss_percent=2.0

    Account: equity=100000, cash=10000, open_positions=[], pnl=0
    Signal:  BUY AAPL at current_price=150.0

    Position sizing (BUY):
      max_allowed_capital = 100000 * (5.0 / 100) = 5000.0
      size_from_equity = floor(5000 / 150) = floor(33.33) = 33
      size_from_cash   = floor(10000 / 150) = floor(66.67) = 66
      position_size    = min(33, 66) = 33

    Stop-loss (BUY):
      stop_loss = round(150 * (1 - 2/100), 2) = round(150 * 0.98, 2) = 147.0

    Validates: Requirements 3.3, 6.2
    """
    signal = make_signal(signal_type="BUY", symbol="AAPL")
    account = make_account(
        equity=100000.0,
        cash=10000.0,
        open_positions=[],
        daily_realized_pnl=0.0,
    )
    config = make_config()

    result = evaluate_signal(signal, 150.0, account, config)

    assert result["decision"] == "APPROVED"
    assert result["signal"] == "BUY"
    assert result["position_size"] == 33
    assert result["stop_loss"] == 147.0
    assert result["symbol"] == "AAPL"
    assert result["timestamp"] == signal["timestamp"]
    assert result["reason"] == "All risk checks passed"


# ---------------------------------------------------------------------------
# APPROVED SELL — specific example (Requirements 3.4, 6.3)
# ---------------------------------------------------------------------------

def test_approved_sell_specific_example():
    """
    Hand-crafted scenario producing an APPROVED SELL with full position
    size and stop_loss.

    Config: max_position_percent=5.0, max_daily_loss=500.0,
            max_open_positions=5, stop_loss_percent=2.0

    Account: equity=100000, cash=5000,
             open_positions=[{symbol: AAPL, size: 25}], pnl=-100.0
    Signal:  SELL AAPL at current_price=150.0

    Position sizing (SELL):
      position_size = 25 (the full held position for AAPL)

    Stop-loss (SELL):
      stop_loss = round(150 * (1 + 2/100), 2) = round(150 * 1.02, 2) = 153.0

    Validates: Requirements 3.4, 6.3
    """
    signal = make_signal(signal_type="SELL", symbol="AAPL")
    account = make_account(
        equity=100000.0,
        cash=5000.0,
        open_positions=[{"symbol": "AAPL", "size": 25}],
        daily_realized_pnl=-100.0,
    )
    config = make_config()

    result = evaluate_signal(signal, 150.0, account, config)

    assert result["decision"] == "APPROVED"
    assert result["signal"] == "SELL"
    assert result["position_size"] == 25
    assert result["stop_loss"] == 153.0
    assert result["symbol"] == "AAPL"
    assert result["timestamp"] == signal["timestamp"]
    assert result["reason"] == "All risk checks passed"


# ---------------------------------------------------------------------------
# Multiple vetoes report first-in-order (Requirement 9.2)
# ---------------------------------------------------------------------------

def test_multiple_vetoes_reports_first():
    """
    Requirement 9.2–9.3: When multiple risk checks would independently
    veto a signal, only the FIRST check in the defined order is reported.

    Defined check order:
      1. Daily loss limit
      2. Max open positions (BUY only)
      3. Position sizing
      4. Stop-loss validation

    In this scenario, BOTH conditions would independently veto:
      - daily_realized_pnl = -600 exceeds max_daily_loss = 500 (check #1)
      - len(open_positions) = 5 meets max_open_positions = 5 (check #2)

    Since daily loss is check #1, the reason should reference daily loss,
    NOT max open positions.

    Validates: Requirements 9.2
    """
    signal = make_signal(signal_type="BUY", symbol="MSFT")
    # Build 5 open positions to trigger the max open positions limit.
    # list comprehension — builds a list by repeating a pattern.
    # Equivalent C++: for (int i = 0; i < 5; ++i) vec.push_back({...});
    positions = [{"symbol": f"SYM{i}", "size": 10} for i in range(5)]
    account = make_account(
        equity=100000.0,
        cash=10000.0,
        open_positions=positions,
        daily_realized_pnl=-600.0,   # Exceeds max_daily_loss=500
    )
    config = make_config()

    result = evaluate_signal(signal, 150.0, account, config)

    assert result["decision"] == "VETOED"
    assert result["position_size"] == 0
    assert result["stop_loss"] is None
    # The reason should reference the daily loss limit (check #1),
    # NOT the max open positions limit (check #2).
    assert "Daily loss" in result["reason"] or "daily" in result["reason"].lower()
    # Verify it mentions the numeric values per Requirement 4.2.
    assert "-600" in result["reason"] or "600" in result["reason"]
    assert "500" in result["reason"]
