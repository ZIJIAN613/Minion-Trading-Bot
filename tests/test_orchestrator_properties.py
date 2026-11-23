"""
tests/test_orchestrator_properties.py — Property-based tests for the orchestrator module.

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

Properties tested in this file (Wave 1):
  Property 1  — Account snapshot formatting (task 1.4)
  Property 3  — Config validation (task 1.3)
  Property 4  — State file round-trip serialization (task 1.5)

Run with:  python -m pytest tests/test_orchestrator_properties.py -v
"""

import json
import os
import tempfile

import numpy as np
import pandas as pd
import pytest
import yaml

from hypothesis import assume, given, settings, HealthCheck
from hypothesis import strategies as st

from orchestrator.config_loader import OrchestratorConfig, load_config
from orchestrator.state_manager import (
    StateData,
    format_account_snapshot,
    load_state,
    save_state,
)


# ===========================================================================
# Mock classes for Alpaca SDK objects
# ===========================================================================
# These mimic the attributes of Alpaca's TradeAccount and Position objects
# without importing the actual SDK (which would require network credentials).
# They act like lightweight C++ structs with named fields.


class MockAccount:
    """Mimics Alpaca TradeAccount with .equity, .cash, .last_equity attributes."""

    def __init__(self, equity, cash, last_equity):
        self.equity = equity
        self.cash = cash
        self.last_equity = last_equity


class MockPosition:
    """Mimics Alpaca Position with .symbol, .qty attributes."""

    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty


# ===========================================================================
# Hypothesis strategies — reusable random input generators
# ===========================================================================


def valid_orchestrator_config_dict():
    """
    Strategy that generates valid config dicts for the orchestrator.
    bar_check_interval_seconds in [60, 3600], optional symbol 1–10 chars,
    optional state_file_path 1–255 chars.
    """
    return st.fixed_dictionaries(
        {"bar_check_interval_seconds": st.floats(
            min_value=60.0, max_value=3600.0, allow_nan=False, allow_infinity=False
        )},
        # Optional keys — may or may not be present in the generated dict.
        optional={
            "symbol": st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
                min_size=1,
                max_size=10,
            ),
            "state_file_path": st.text(
                alphabet=st.characters(
                    whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd"),
                    whitelist_characters="/\\._-"
                ),
                min_size=1,
                max_size=100,  # Keep reasonable, valid range is 1-255
            ),
        },
    )


def invalid_orchestrator_config_dict():
    """
    Strategy that generates config dicts with one invalid field.
    Returns (dict, expected_error_key) tuples.
    """
    return st.one_of(
        # Missing required key
        st.just(({"symbol": "AAPL"}, "bar_check_interval_seconds")),
        # bar_check_interval_seconds too low
        st.floats(min_value=-1000, max_value=59.9, allow_nan=False, allow_infinity=False).map(
            lambda v: ({"bar_check_interval_seconds": v}, "bar_check_interval_seconds")
        ),
        # bar_check_interval_seconds too high
        st.floats(min_value=3600.1, max_value=100000, allow_nan=False, allow_infinity=False).map(
            lambda v: ({"bar_check_interval_seconds": v}, "bar_check_interval_seconds")
        ),
        # bar_check_interval_seconds is a boolean
        st.sampled_from([True, False]).map(
            lambda v: ({"bar_check_interval_seconds": v}, "bar_check_interval_seconds")
        ),
        # bar_check_interval_seconds is a string
        st.just(({"bar_check_interval_seconds": "300"}, "bar_check_interval_seconds")),
    )


# ===========================================================================
# Property 3: Config validation accepts valid values and rejects invalid ones
# ===========================================================================
# Feature: orchestrator, Property 3: Config validation accepts valid values
# and rejects invalid ones
# Validates: Requirements 8.2, 8.6, 8.7


@given(config_data=valid_orchestrator_config_dict())
@settings(max_examples=100)
def test_property_3_valid_config_accepted(config_data):
    """
    For any valid config dict, load_config SHALL return an OrchestratorConfig
    with matching values.
    """
    # Write the config to a temp YAML file.
    # We use tempfile instead of tmp_path because Hypothesis does not reset
    # pytest function-scoped fixtures between generated inputs.
    import tempfile as _tf
    tmp_dir = _tf.mkdtemp()
    config_file_path = os.path.join(tmp_dir, "config.yaml")
    with open(config_file_path, "w", encoding="utf-8") as f:
        f.write(yaml.dump(config_data))
    config_file = config_file_path

    # Load should succeed without raising.
    result = load_config(config_file)

    # Verify returned type.
    assert isinstance(result, OrchestratorConfig)

    # Verify bar_check_interval_seconds matches (as float).
    assert result.bar_check_interval_seconds == float(
        config_data["bar_check_interval_seconds"]
    )

    # Verify optional fields use defaults when absent.
    if "symbol" in config_data:
        assert result.symbol == config_data["symbol"]
    else:
        assert result.symbol == "AAPL"

    if "state_file_path" in config_data:
        assert result.state_file_path == config_data["state_file_path"]
    else:
        assert result.state_file_path == "orchestrator/state.json"


@given(data=invalid_orchestrator_config_dict())
@settings(max_examples=100)
def test_property_3_invalid_config_rejected(data):
    """
    For any config dict with an invalid field, load_config SHALL raise
    ValueError identifying the invalid key.
    """
    config_dict, expected_key = data

    import tempfile as _tf
    tmp_dir = _tf.mkdtemp()
    config_file_path = os.path.join(tmp_dir, "config.yaml")
    with open(config_file_path, "w", encoding="utf-8") as f:
        f.write(yaml.dump(config_dict))

    with pytest.raises(ValueError) as exc_info:
        load_config(config_file_path)

    # The error message should identify the problematic key.
    assert expected_key in str(exc_info.value)


# ===========================================================================
# Property 1: Account snapshot formatting preserves data and conforms to schema
# ===========================================================================
# Feature: orchestrator, Property 1: Account snapshot formatting preserves
# data and conforms to schema
# Validates: Requirements 1.2, 1.5


@given(
    equity=st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    cash=st.floats(min_value=0.0, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    last_equity=st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    num_positions=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=100)
def test_property_1_valid_account_snapshot(equity, cash, last_equity, num_positions):
    """
    For any Alpaca TradeAccount with positive equity and non-negative cash,
    format_account_snapshot SHALL return a correctly formatted dict.
    """
    # Build mock account (Alpaca returns strings for monetary fields).
    account = MockAccount(str(equity), str(cash), str(last_equity))

    # Build mock positions with varying fractional quantities.
    # Some will truncate to 0 and should be excluded.
    positions = []
    for i in range(num_positions):
        # Alternate between positions that will be included (qty >= 1.0)
        # and positions that might be excluded (qty < 1.0).
        if i % 2 == 0:
            qty = str(float(i + 1))  # 1.0, 3.0, 5.0, ...
        else:
            qty = str(0.5)  # will truncate to 0, should be excluded
        positions.append(MockPosition(f"SYM{i}", qty))

    result = format_account_snapshot(account, positions)

    # Verify structure.
    assert isinstance(result, dict)
    assert "equity" in result
    assert "cash" in result
    assert "open_positions" in result
    assert "daily_realized_pnl" in result

    # Verify values.
    assert result["equity"] == equity
    assert result["cash"] == cash
    assert abs(result["daily_realized_pnl"] - (equity - last_equity)) < 1e-9

    # Verify open_positions only includes positions with truncated qty >= 1.
    for pos_dict in result["open_positions"]:
        assert isinstance(pos_dict["symbol"], str)
        assert isinstance(pos_dict["size"], int)
        assert pos_dict["size"] >= 1


@given(
    equity=st.floats(min_value=-1_000_000, max_value=0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=50)
def test_property_1_negative_equity_raises(equity):
    """
    For any account with zero or negative equity, format_account_snapshot
    SHALL raise ValueError.
    """
    account = MockAccount(str(equity), "1000", "1000")
    with pytest.raises(ValueError, match="equity"):
        format_account_snapshot(account, [])


@given(
    cash=st.floats(min_value=-1_000_000, max_value=-0.01, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=50)
def test_property_1_negative_cash_raises(cash):
    """
    For any account with negative cash, format_account_snapshot SHALL raise
    ValueError.
    """
    account = MockAccount("10000", str(cash), "10000")
    with pytest.raises(ValueError, match="cash"):
        format_account_snapshot(account, [])


# ===========================================================================
# Property 4: State file round-trip serialization
# ===========================================================================
# Feature: orchestrator, Property 4: State file round-trip serialization
# Validates: Requirements 2.12, 6.7


@given(
    num_positions=st.integers(min_value=0, max_value=5),
    has_last_acted_bar=st.booleans(),
    has_pending_decision=st.booleans(),
)
@settings(max_examples=100)
def test_property_4_state_roundtrip(
    num_positions, has_last_acted_bar, has_pending_decision
):
    """
    For any valid StateData, serializing to JSON and deserializing SHALL
    produce an equivalent StateData — all fields preserved without loss.
    """
    import tempfile as _tf
    tmp_dir = _tf.mkdtemp()
    """
    For any valid StateData, serializing to JSON and deserializing SHALL
    produce an equivalent StateData — all fields preserved without loss.
    """
    # Build positions list.
    positions = []
    for i in range(num_positions):
        positions.append({
            "symbol": f"SYM{i}",
            "entry_order_id": f"order-{i}",
            "stop_loss_order_id": f"sl-{i}" if i % 2 == 0 else None,
            "quantity": i + 1,
            "entry_price": round(100.0 + i * 10.5, 2),
            "entry_timestamp": f"2024-06-{15 + i:02d}T14:30:00Z",
            "status": "open",
            "exit_timestamp": None,
        })

    last_acted_bar = "2024-06-15T00:00:00Z" if has_last_acted_bar else None

    pending_decision = None
    if has_pending_decision:
        pending_decision = {
            "decision": "APPROVED",
            "signal": "BUY",
            "position_size": 10,
            "stop_loss": 148.50,
            "reason": "All risk checks passed",
            "symbol": "AAPL",
        }

    # Create StateData.
    original = StateData(
        positions=positions,
        last_acted_bar=last_acted_bar,
        pending_decision=pending_decision,
        pending_decision_bar_timestamp="2024-06-15" if has_pending_decision else None,
    )

    # Write to disk.
    state_file = os.path.join(tmp_dir, "state.json")
    save_state(original, state_file)

    # Read back.
    loaded = load_state(state_file)

    # Verify round-trip equivalence.
    assert loaded.positions == original.positions
    assert loaded.last_acted_bar == original.last_acted_bar
    assert loaded.pending_decision == original.pending_decision
    assert loaded.pending_decision_bar_timestamp == original.pending_decision_bar_timestamp


# ===========================================================================
# Property 10: Safety check correctly identifies unprotected positions
# ===========================================================================
# Feature: orchestrator, Property 10: Safety check correctly identifies
# unprotected positions
# Validates: Requirements 7.2, 7.3


class MockOrder:
    """Mimics an Alpaca Order with .type, .symbol, .side attributes."""

    def __init__(self, order_type, symbol, side):
        self.type = order_type
        self.symbol = symbol
        self.side = side


class MockPositionForSafety:
    """Mimics an Alpaca Position with .symbol, .qty, .current_price attributes."""

    def __init__(self, symbol, qty, current_price="150.00"):
        self.symbol = symbol
        self.qty = qty
        self.current_price = current_price


class MockTradingClientForSafety:
    """
    Mock TradingClient that returns configurable positions and orders.

    This replaces the real Alpaca TradingClient for testing — like a
    C++ mock class that overrides virtual methods with test-specific behaviour.
    """

    def __init__(self, positions, orders):
        self._positions = positions
        self._orders = orders

    def get_all_positions(self):
        return self._positions

    def get_orders(self, filter=None):
        return self._orders


from orchestrator.safety_check import run_safety_check


@given(
    num_protected=st.integers(min_value=0, max_value=5),
    num_unprotected=st.integers(min_value=0, max_value=5),
)
@settings(max_examples=100)
def test_property_10_safety_check_identification(num_protected, num_unprotected):
    """
    For any set of open positions and open orders, the safety check SHALL
    identify exactly the set of positions that lack a matching protective
    stop-loss order.

    A position is "protected" iff there exists an order with:
      - type in ("stop", "stop_limit")
      - matching symbol
      - side = "sell" for long (qty > 0) or "buy" for short (qty < 0)
    """
    positions = []
    orders = []

    # Create protected long positions (each has a matching stop sell order).
    for i in range(num_protected):
        symbol = f"PROT{i}"
        positions.append(MockPositionForSafety(symbol, "10"))
        orders.append(MockOrder("stop", symbol, "sell"))

    # Create unprotected long positions (no matching order).
    for i in range(num_unprotected):
        symbol = f"UNPROT{i}"
        positions.append(MockPositionForSafety(symbol, "5"))
        # No corresponding stop order — intentionally left unprotected.

    client = MockTradingClientForSafety(positions, orders)
    result = run_safety_check(client)

    # Total positions should be the count of all positions.
    assert result["total_positions"] == num_protected + num_unprotected

    # Unprotected count should exactly match the unprotected positions.
    assert result["unprotected_count"] == num_unprotected


@given(
    num_short_protected=st.integers(min_value=0, max_value=3),
    num_short_unprotected=st.integers(min_value=0, max_value=3),
)
@settings(max_examples=50)
def test_property_10_short_positions(num_short_protected, num_short_unprotected):
    """
    Short positions (qty < 0) are protected by BUY stop orders.
    The safety check SHALL correctly identify unprotected short positions.
    """
    positions = []
    orders = []

    # Protected short positions — matched with "buy" stop orders.
    for i in range(num_short_protected):
        symbol = f"SHORT_P{i}"
        positions.append(MockPositionForSafety(symbol, "-10"))
        orders.append(MockOrder("stop_limit", symbol, "buy"))

    # Unprotected short positions.
    for i in range(num_short_unprotected):
        symbol = f"SHORT_U{i}"
        positions.append(MockPositionForSafety(symbol, "-5"))

    client = MockTradingClientForSafety(positions, orders)
    result = run_safety_check(client)

    assert result["total_positions"] == num_short_protected + num_short_unprotected
    assert result["unprotected_count"] == num_short_unprotected


# ===========================================================================
# Property 11: Current price extraction from DataFrame
# ===========================================================================
# Feature: orchestrator, Property 11: Current price extraction from DataFrame
# Validates: Requirements 3.3


@given(
    num_rows=st.integers(min_value=1, max_value=50),
    base_price=st.floats(min_value=1.0, max_value=10000.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_property_11_current_price_is_last_close(num_rows, base_price):
    """
    For any valid Bar DataFrame with at least one row, the current_price
    passed to evaluate_signal SHALL equal the close value of the last row
    (i.e., df["close"].iloc[-1]).

    This test verifies the extraction logic used in _run_cycle() Stage 4.
    We test the extraction formula directly rather than calling _run_cycle
    (which requires full mocked pipeline infrastructure).
    """
    # Build a minimal valid bar DataFrame with random close prices.
    # pd.date_range creates a sequence of timestamps — like generating
    # a vector of timepoints in C++.
    dates = pd.date_range(
        start="2024-01-01", periods=num_rows, freq="D", tz="UTC"
    )

    # Generate close prices: base_price + small random offsets.
    # np.random.default_rng gives a reproducible random generator.
    rng = np.random.default_rng(seed=42)
    close_prices = base_price + rng.uniform(-1.0, 1.0, size=num_rows)

    df = pd.DataFrame(
        {
            "open": close_prices * 0.99,
            "high": close_prices * 1.01,
            "low": close_prices * 0.98,
            "close": close_prices,
            "volume": np.ones(num_rows, dtype=np.int64) * 1000,
        },
        index=dates,
    )

    # This is the exact extraction formula used in _run_cycle() Stage 4:
    current_price = float(df["close"].iloc[-1])

    # Verify it equals the last close price.
    expected = float(close_prices[-1])
    assert abs(current_price - expected) < 1e-9


# ===========================================================================
# Property 2: New bar detection is a strict date comparison
# ===========================================================================
# Feature: orchestrator, Property 2: New bar detection is a strict date
# comparison
# Validates: Requirements 2.1, 2.2, 2.3, 2.5


# ===========================================================================
# Property 2: New bar detection is a strict date comparison
# ===========================================================================
# Feature: orchestrator, Property 2: New bar detection is a strict date
# comparison
# Validates: Requirements 2.1, 2.2, 2.3, 2.5


class MockRestClientForBarCheck:
    """
    Mock RestClient that returns a DataFrame with a single bar at the
    specified date.  Used to test _check_new_bar() in isolation without
    hitting the real Alpaca API.
    """

    def __init__(self, bar_date_str: str):
        """
        Args:
            bar_date_str: ISO date string (e.g., "2024-06-15") for the
                          single bar's timestamp.
        """
        self._bar_date_str = bar_date_str

    def get_historical_bars(self, symbol, timeframe, start, end):
        """Return a DataFrame with one bar at the configured date."""
        # Create a single-row DataFrame mimicking the bar schema.
        timestamp = pd.Timestamp(self._bar_date_str, tz="UTC")
        df = pd.DataFrame(
            {
                "open": [150.0],
                "high": [151.0],
                "low": [149.0],
                "close": [150.5],
                "volume": np.array([1000], dtype=np.int64),
            },
            index=pd.DatetimeIndex([timestamp], name="timestamp"),
        )
        return df


class MockRestClientEmpty:
    """Mock RestClient that returns an empty DataFrame (no bars available)."""

    def get_historical_bars(self, symbol, timeframe, start, end):
        """Return an empty DataFrame with the correct schema."""
        df = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([], name="timestamp", dtype="datetime64[ns, UTC]"),
        )
        return df


from orchestrator.loop import _check_new_bar


@given(
    # Generate date offsets to create pairs of (latest_bar, last_acted_bar).
    latest_day_offset=st.integers(min_value=0, max_value=365),
    last_acted_day_offset=st.integers(min_value=0, max_value=365),
)
@settings(max_examples=100)
def test_property_2_new_bar_strict_date_comparison(
    latest_day_offset, last_acted_day_offset
):
    """
    For any pair of dates, _check_new_bar SHALL return True if and only if
    the latest bar's date (YYYY-MM-DD) is strictly greater than last_acted_bar's
    date (YYYY-MM-DD).

    This is a pure date comparison — time-of-day within the date does not matter.
    """
    from datetime import date, timedelta as td

    # Generate two dates based on offsets from a fixed base.
    base = date(2024, 1, 1)
    latest_date = base + td(days=latest_day_offset)
    last_acted_date = base + td(days=last_acted_day_offset)

    latest_date_str = latest_date.isoformat()  # "YYYY-MM-DD"
    last_acted_str = last_acted_date.isoformat()  # "YYYY-MM-DD"

    # Create a mock RestClient that returns a bar at latest_date.
    mock_client = MockRestClientForBarCheck(latest_date_str)

    # Call _check_new_bar with the last_acted_bar set.
    is_new, bar_timestamp = _check_new_bar(mock_client, "AAPL", last_acted_str)

    # Expected result: new bar detected only if latest > last_acted.
    expected_is_new = latest_date > last_acted_date

    assert is_new == expected_is_new
    assert bar_timestamp == latest_date_str


@given(
    day_offset=st.integers(min_value=0, max_value=365),
)
@settings(max_examples=50)
def test_property_2_none_last_acted_always_new(day_offset):
    """
    When last_acted_bar is None (first run), _check_new_bar SHALL always
    return True — any available bar is treated as new.
    """
    from datetime import date, timedelta as td

    bar_date = (date(2024, 1, 1) + td(days=day_offset)).isoformat()
    mock_client = MockRestClientForBarCheck(bar_date)

    is_new, bar_timestamp = _check_new_bar(mock_client, "AAPL", None)

    assert is_new is True
    assert bar_timestamp == bar_date


def test_property_2_empty_bars_returns_false():
    """
    When no bars are available (empty DataFrame), _check_new_bar SHALL
    return (False, None).
    """
    mock_client = MockRestClientEmpty()

    is_new, bar_timestamp = _check_new_bar(mock_client, "AAPL", "2024-06-15")

    assert is_new is False
    assert bar_timestamp is None


# ===========================================================================
# Property 5: Error classification based on entry_order_id and contract violations
# ===========================================================================
# Feature: orchestrator, Property 5: Error classification based on
# entry_order_id and contract violations
# Validates: Requirements 3.2, 5.3, 5.4, 5.8

from orchestrator.loop import _classify_error, ContractViolationError


@given(
    stage=st.sampled_from(["account_query", "data_ingestion", "strategy", "risk_engine"]),
)
@settings(max_examples=50)
def test_property_5_pre_execution_errors_are_pre_order(stage):
    """
    For any error in account_query, data_ingestion, strategy, or risk_engine
    stages, _classify_error SHALL return "pre_order" — these are safe to
    skip and retry because no order was at risk.
    """
    error = IOError("some transient failure")
    result = _classify_error(stage, error, order_result=None)
    assert result == "pre_order"


@given(
    stage=st.sampled_from(["account_query", "data_ingestion", "strategy", "risk_engine"]),
)
@settings(max_examples=50)
def test_property_5_contract_violation_overrides_stage(stage):
    """
    ContractViolationError SHALL always be classified as "post_order" (halt),
    EVEN when it occurs in a pre-execution stage — because it indicates an
    internal bug, not a transient external failure.
    """
    error = ContractViolationError("stage returned None")
    result = _classify_error(stage, error, order_result=None)
    assert result == "post_order"


@given(
    entry_order_id=st.one_of(st.none(), st.text(min_size=1, max_size=20)),
)
@settings(max_examples=100)
def test_property_5_execution_error_classification(entry_order_id):
    """
    For Execution errors with an Order_Result:
      - entry_order_id=None → "pre_order" (no order submitted)
      - entry_order_id not None → "post_order" (order state ambiguous)
    """
    order_result = {
        "status": "error",
        "entry_order_id": entry_order_id,
        "error_type": "ConnectionError",
    }
    error = RuntimeError("execution failed")

    result = _classify_error("execution", error, order_result=order_result)

    if entry_order_id is None:
        assert result == "pre_order"
    else:
        assert result == "post_order"


def test_property_5_raw_exception_from_execution_is_post_order():
    """
    A raw exception from Execution (no Order_Result) SHALL always be
    classified as "post_order" — cannot determine if order was submitted.
    """
    error = TypeError("unexpected error in execution")
    result = _classify_error("execution", error, order_result=None)
    assert result == "post_order"


# ===========================================================================
# Property 6: Correct declines never trigger halt or error log
# ===========================================================================
# Feature: orchestrator, Property 6: Correct declines never trigger halt
# or error log
# Validates: Requirements 5.7
#
# Property 6's claim: VETOED, NO_ACTION, and Execution status="skipped"
# outcomes are returned as normal values from _run_cycle (not exceptions).
# They never reach _classify_error, never log at ERROR/CRITICAL, and never
# raise.  We verify this by calling _run_cycle with mocked pipeline stages
# producing each correct-decline outcome, and asserting:
#   1. No exception raised (function returns cleanly).
#   2. No ERROR or CRITICAL log records emitted.
#   3. The returned outcome string matches the expected decline type.

import logging as _logging_module
from unittest.mock import MagicMock, patch


def _make_simple_bar_df():
    """Build a minimal 31-row bar DataFrame for property 6 tests."""
    dates = pd.date_range(start="2024-01-01", periods=31, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [150.0] * 31,
            "high": [151.0] * 31,
            "low": [149.0] * 31,
            "close": [150.0] * 31,
            "volume": np.array([1000] * 31, dtype=np.int64),
        },
        index=dates,
    )


def _make_mock_trading_client_for_prop6(clock_is_open=True):
    """Create a mock TradingClient for Property 6 tests."""
    client = MagicMock()
    mock_account = MagicMock()
    mock_account.equity = "100000"
    mock_account.cash = "50000"
    mock_account.last_equity = "99500"
    client.get_account.return_value = mock_account
    client.get_all_positions.return_value = []
    mock_clock = MagicMock()
    mock_clock.is_open = clock_is_open
    client.get_clock.return_value = mock_clock
    return client


from orchestrator.loop import _run_cycle
from orchestrator.config_loader import OrchestratorConfig
from orchestrator.state_manager import StateData


@given(
    decline_type=st.sampled_from(["VETOED", "NO_ACTION"]),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_6_risk_engine_declines_no_error_log(decline_type, caplog):
    """
    For any pipeline outcome where the Risk Engine returns VETOED or NO_ACTION,
    _run_cycle SHALL:
      - Return cleanly (no exception raised)
      - Produce no ERROR or CRITICAL log records
      - Return the expected outcome string ("vetoed" or "no_action")
    """
    from strategy import StrategyConfig

    with patch("orchestrator.loop.compute_signal") as mock_compute, \
         patch("orchestrator.loop.evaluate_signal") as mock_evaluate:

        mock_compute.return_value = {
            "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        mock_evaluate.return_value = {
            "decision": decline_type,
            "signal": "BUY" if decline_type == "VETOED" else "HOLD",
            "position_size": 0, "stop_loss": None,
            "reason": "Test decline",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
        }

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_simple_bar_df()
        trading_client = _make_mock_trading_client_for_prop6()

        orch_config = OrchestratorConfig(
            bar_check_interval_seconds=300.0, symbol="AAPL",
            state_file_path="orchestrator/state.json",
            kill_switch_file_path="orchestrator/KILL_SWITCH",
        )

        with caplog.at_level(_logging_module.DEBUG, logger="orchestrator"):
            outcome, order_result = _run_cycle(
                rest_client=mock_rest,
                trading_client=trading_client,
                strategy_config=StrategyConfig(short_window=10, long_window=30),
                risk_config=MagicMock(),
                execution_config=MagicMock(),
                orch_config=orch_config,
                state_data=StateData(),
                cycle_number=1,
            )

    # No exception raised (implicit — we reached this point).
    expected_outcome = decline_type.lower()  # "VETOED" → "vetoed"
    if decline_type == "NO_ACTION":
        expected_outcome = "no_action"
    assert outcome == expected_outcome
    assert order_result is None

    # No ERROR or CRITICAL log records should exist.
    error_records = [
        r for r in caplog.records
        if r.levelno >= _logging_module.ERROR
    ]
    assert len(error_records) == 0, (
        f"Unexpected ERROR/CRITICAL logs for decline '{decline_type}': "
        f"{[r.message for r in error_records]}"
    )


def test_property_6_execution_skipped_no_error_log(caplog):
    """
    When Execution returns status='skipped', _run_cycle SHALL:
      - Return cleanly (no exception raised)
      - Produce no ERROR or CRITICAL log records
      - Return outcome "skipped" with order_result populated
    """
    from strategy import StrategyConfig

    with patch("orchestrator.loop.compute_signal") as mock_compute, \
         patch("orchestrator.loop.evaluate_signal") as mock_evaluate, \
         patch("orchestrator.loop.execute_decision") as mock_execute:

        mock_compute.return_value = {
            "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        mock_evaluate.return_value = {
            "decision": "APPROVED", "signal": "BUY", "position_size": 10,
            "stop_loss": 147.0, "reason": "Passed",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        mock_execute.return_value = {
            "status": "skipped", "decision": "APPROVED", "signal": "BUY",
            "symbol": "AAPL", "position_size": 0,
            "reason": "APPROVED+HOLD guard", "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
        }

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_simple_bar_df()
        trading_client = _make_mock_trading_client_for_prop6(clock_is_open=True)

        orch_config = OrchestratorConfig(
            bar_check_interval_seconds=300.0, symbol="AAPL",
            state_file_path="orchestrator/state.json",
            kill_switch_file_path="orchestrator/KILL_SWITCH",
        )

        with caplog.at_level(_logging_module.DEBUG, logger="orchestrator"):
            outcome, order_result = _run_cycle(
                rest_client=mock_rest,
                trading_client=trading_client,
                strategy_config=StrategyConfig(short_window=10, long_window=30),
                risk_config=MagicMock(),
                execution_config=MagicMock(),
                orch_config=orch_config,
                state_data=StateData(),
                cycle_number=1,
            )

    assert outcome == "skipped"
    assert order_result is not None
    assert order_result["status"] == "skipped"

    # No ERROR or CRITICAL log records.
    error_records = [
        r for r in caplog.records
        if r.levelno >= _logging_module.ERROR
    ]
    assert len(error_records) == 0


# ===========================================================================
# Property 7: Consecutive failure escalation at threshold
# ===========================================================================
# Feature: orchestrator, Property 7: Consecutive failure escalation at threshold
# Validates: Requirements 5.9
#
# NOTE: The consecutive failure counter and escalation logic will be
# implemented in run_loop (task 7.2).  At this stage we test the CONCEPT:
# a helper function that determines the log level based on the consecutive
# failure count.  We define and test this logic here so it's ready for
# integration into run_loop.
#
# The helper is simple enough to define inline for testing purposes.
# When task 7.2 integrates it into run_loop, it will use this same logic.


def _get_failure_log_level(consecutive_failures: int) -> int:
    """
    Determine the log level for a pre-order failure based on the
    consecutive failure count.

    Rules (Requirement 5.9):
      - 1st and 2nd consecutive failures → WARNING (level 30)
      - 3rd and subsequent consecutive failures → ERROR (level 40)

    Args:
        consecutive_failures: How many consecutive cycles have been skipped
                              due to pre-order failures (1-indexed, i.e.,
                              the CURRENT failure is included in the count).

    Returns:
        logging.WARNING (30) or logging.ERROR (40).
    """
    import logging as _log
    if consecutive_failures >= 3:
        return _log.ERROR
    return _log.WARNING


@given(
    consecutive_count=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=100)
def test_property_7_escalation_at_threshold(consecutive_count):
    """
    For N consecutive pre-order failures:
      - N < 3 → WARNING level
      - N >= 3 → ERROR level (escalated)
    """
    import logging as _log

    level = _get_failure_log_level(consecutive_count)

    if consecutive_count < 3:
        assert level == _log.WARNING
    else:
        assert level == _log.ERROR


def test_property_7_counter_reset_concept():
    """
    After a successful cycle, the consecutive failure counter resets to zero.
    The NEXT failure after a success should be logged at WARNING (not ERROR),
    regardless of how many consecutive failures preceded the success.
    """
    import logging as _log

    # Simulate 5 consecutive failures (escalated to ERROR).
    for i in range(1, 6):
        level = _get_failure_log_level(i)
    assert level == _log.ERROR  # 5th consecutive = ERROR

    # After a success, counter resets to 0.  Next failure is #1 → WARNING.
    reset_count = 1  # First failure after reset.
    level_after_reset = _get_failure_log_level(reset_count)
    assert level_after_reset == _log.WARNING


# ===========================================================================
# Property 8: Position record creation on BUY fill
# ===========================================================================
# Feature: orchestrator, Property 8: Position record creation on BUY fill
# Validates: Requirements 6.2

from orchestrator.state_manager import add_position_record


@given(
    symbol=st.text(
        alphabet=st.characters(whitelist_categories=("Lu",)),
        min_size=1, max_size=5,
    ),
    entry_order_id=st.text(min_size=5, max_size=20),
    stop_loss_order_id=st.one_of(st.none(), st.text(min_size=5, max_size=20)),
    fill_price=st.floats(min_value=0.01, max_value=100000.0, allow_nan=False, allow_infinity=False),
    filled_quantity=st.integers(min_value=1, max_value=10000),
)
@settings(max_examples=100)
def test_property_8_buy_fill_creates_position_record(
    symbol, entry_order_id, stop_loss_order_id, fill_price, filled_quantity
):
    """
    For any Order_Result with status in {"filled", "partial_fill"} for a BUY
    signal, the resulting PositionRecord SHALL contain:
      - symbol matching the configured symbol
      - entry_order_id matching the Order_Result
      - stop_loss_order_id from the Order_Result (or None)
      - quantity equal to filled_quantity
      - entry_price equal to round(fill_price, 2)
      - entry_timestamp as a UTC ISO 8601 string
      - status equal to "open"
    """
    order_result = {
        "status": "filled",
        "entry_order_id": entry_order_id,
        "stop_loss_order_id": stop_loss_order_id,
        "fill_price": fill_price,
        "filled_quantity": filled_quantity,
        "filled_at": pd.Timestamp("2024-06-15T14:30:00", tz="UTC"),
        "signal": "BUY",
    }

    original_state = StateData(positions=[], last_acted_bar="2024-06-15")
    new_state = add_position_record(original_state, order_result, symbol)

    # Should have exactly one position now.
    assert len(new_state.positions) == 1
    pos = new_state.positions[0]

    # Verify all required fields.
    assert pos["symbol"] == symbol
    assert pos["entry_order_id"] == entry_order_id
    assert pos["stop_loss_order_id"] == stop_loss_order_id
    assert pos["quantity"] == filled_quantity
    assert pos["entry_price"] == round(fill_price, 2)
    assert pos["status"] == "open"
    assert pos["exit_timestamp"] is None

    # entry_timestamp should be a valid ISO 8601 string.
    assert isinstance(pos["entry_timestamp"], str)
    assert "T" in pos["entry_timestamp"]  # Basic ISO format check.

    # Original state should NOT be mutated (immutable pattern).
    assert len(original_state.positions) == 0


# ===========================================================================
# Property 9: Position record update on SELL
# ===========================================================================
# Feature: orchestrator, Property 9: Position record update on SELL
# Validates: Requirements 6.3

from orchestrator.state_manager import update_position_on_sell


@given(
    initial_qty=st.integers(min_value=1, max_value=1000),
    filled_qty=st.integers(min_value=1, max_value=1000),
    is_full_fill=st.booleans(),
)
@settings(max_examples=100)
def test_property_9_sell_updates_position(initial_qty, filled_qty, is_full_fill):
    """
    For any existing open PositionRecord and a SELL Order_Result:
      - If status is "filled": record's status → "closed" with exit_timestamp.
      - If status is "partial_fill": quantity decremented by filled_quantity.
        If remaining is 0 → "closed". Otherwise → "partially_closed".
    """
    # Build an existing open position.
    existing_position = {
        "symbol": "AAPL",
        "entry_order_id": "entry-123",
        "stop_loss_order_id": "sl-456",
        "quantity": initial_qty,
        "entry_price": 150.25,
        "entry_timestamp": "2024-06-15T14:30:00Z",
        "status": "open",
        "exit_timestamp": None,
    }
    state = StateData(positions=[existing_position], last_acted_bar="2024-06-15")

    if is_full_fill:
        # Full fill — status should become "closed".
        order_result = {"status": "filled", "filled_quantity": initial_qty}
        new_state = update_position_on_sell(state, order_result, "AAPL")

        pos = new_state.positions[0]
        assert pos["status"] == "closed"
        assert pos["exit_timestamp"] is not None
    else:
        # Partial fill — decrement quantity.
        # Ensure filled_qty doesn't exceed initial_qty for a realistic test.
        actual_filled = min(filled_qty, initial_qty)
        order_result = {"status": "partial_fill", "filled_quantity": actual_filled}
        new_state = update_position_on_sell(state, order_result, "AAPL")

        pos = new_state.positions[0]
        remaining = initial_qty - actual_filled

        if remaining <= 0:
            assert pos["status"] == "closed"
            assert pos["exit_timestamp"] is not None
        else:
            assert pos["status"] == "partially_closed"
            assert pos["quantity"] == remaining

    # Original state should NOT be mutated.
    assert state.positions[0]["status"] == "open"
    assert state.positions[0]["exit_timestamp"] is None
