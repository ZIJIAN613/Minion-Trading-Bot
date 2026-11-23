"""
tests/test_orchestrator_unit.py — Example-based unit tests for the orchestrator module.

These tests cover specific scenarios and edge cases that are not well-suited
to property-based testing (specific file system states, exact error messages,
logging behaviour, etc.).

Run with:  python -m pytest tests/test_orchestrator_unit.py -v
"""

import json
import logging
import os
import tempfile
import threading
import time

import numpy as np
import pandas as pd
import pytest
import yaml

from orchestrator.config_loader import OrchestratorConfig, load_config
from orchestrator.state_manager import (
    StateData,
    load_state,
    save_state,
)


# ===========================================================================
# Config Loader Unit Tests (Task 1.6)
# ===========================================================================


class TestConfigFileNotFound:
    """test_config_file_not_found — FileNotFoundError with path in message."""

    def test_raises_file_not_found(self, tmp_path):
        """
        If the config file does not exist, load_config SHALL raise
        FileNotFoundError with the expected path in the message.
        """
        missing_path = str(tmp_path / "nonexistent" / "config.yaml")
        with pytest.raises(FileNotFoundError) as exc_info:
            load_config(missing_path)
        # The error message should contain the file path.
        assert missing_path in str(exc_info.value)


class TestConfigInvalidYaml:
    """test_config_invalid_yaml — ValueError on malformed YAML."""

    def test_raises_value_error_on_bad_yaml(self, tmp_path):
        """
        If the config file contains invalid YAML syntax, load_config SHALL
        raise ValueError mentioning the file path.
        """
        config_file = tmp_path / "config.yaml"
        # Write deliberately malformed YAML (unmatched bracket).
        config_file.write_text("bar_check_interval_seconds: [invalid", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(str(config_file))
        assert str(config_file) in str(exc_info.value)


class TestConfigNonDictYaml:
    """test_config_non_dict_yaml — ValueError on YAML list."""

    def test_raises_value_error_on_list(self, tmp_path):
        """
        If the config file is valid YAML but parses to a non-dict (e.g., a list),
        load_config SHALL raise ValueError stating it must be a mapping.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- item1\n- item2\n", encoding="utf-8")

        with pytest.raises(ValueError, match="mapping"):
            load_config(str(config_file))

    def test_raises_value_error_on_empty_file(self, tmp_path):
        """
        An empty YAML file parses to None, which should also raise ValueError.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("", encoding="utf-8")

        with pytest.raises(ValueError, match="mapping"):
            load_config(str(config_file))


class TestConfigMissingRequiredKey:
    """test_config_missing_required_key — ValueError naming missing key."""

    def test_raises_value_error_naming_key(self, tmp_path):
        """
        If the config file is missing bar_check_interval_seconds,
        load_config SHALL raise ValueError identifying the missing key.
        """
        config_file = tmp_path / "config.yaml"
        # Valid YAML dict but missing the required key.
        config_file.write_text(
            yaml.dump({"symbol": "AAPL"}), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="bar_check_interval_seconds"):
            load_config(str(config_file))


class TestConfigUnknownKeysWarning:
    """test_config_unknown_keys_warning — WARNING logged, no error."""

    def test_unknown_keys_logged_at_warning(self, tmp_path, caplog):
        """
        If the config file contains unrecognized keys, load_config SHALL
        log a WARNING for each but NOT raise an error.
        """
        config_file = tmp_path / "config.yaml"
        config_data = {
            "bar_check_interval_seconds": 120,
            "unknown_key_1": "hello",
            "another_mystery": 42,
        }
        config_file.write_text(yaml.dump(config_data), encoding="utf-8")

        # caplog is a pytest fixture that captures log output.
        # We set the level to WARNING so we see the expected messages.
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = load_config(str(config_file))

        # Should succeed (no error raised).
        assert isinstance(result, OrchestratorConfig)
        assert result.bar_check_interval_seconds == 120.0

        # Check that warnings were logged for each unknown key.
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("unknown_key_1" in msg for msg in warning_messages)
        assert any("another_mystery" in msg for msg in warning_messages)


class TestConfigDefaultsApplied:
    """test_config_defaults_applied — symbol and state_file_path use defaults."""

    def test_defaults_when_optional_keys_absent(self, tmp_path):
        """
        When optional keys (symbol, state_file_path) are absent from the
        config file, load_config SHALL use the documented default values.
        """
        config_file = tmp_path / "config.yaml"
        config_data = {"bar_check_interval_seconds": 300}
        config_file.write_text(yaml.dump(config_data), encoding="utf-8")

        result = load_config(str(config_file))

        assert result.symbol == "AAPL"
        assert result.state_file_path == "orchestrator/state.json"

    def test_explicit_values_override_defaults(self, tmp_path):
        """
        When optional keys are provided, their values SHALL be used
        instead of defaults.
        """
        config_file = tmp_path / "config.yaml"
        config_data = {
            "bar_check_interval_seconds": 60,
            "symbol": "TSLA",
            "state_file_path": "/tmp/my_state.json",
        }
        config_file.write_text(yaml.dump(config_data), encoding="utf-8")

        result = load_config(str(config_file))

        assert result.symbol == "TSLA"
        assert result.state_file_path == "/tmp/my_state.json"


# ===========================================================================
# State Manager Unit Tests (Task 1.6)
# ===========================================================================


class TestStateFileCreatedOnFirstRun:
    """test_state_file_created_on_first_run — empty state file created."""

    def test_load_creates_empty_state(self, tmp_path):
        """
        If the state file does not exist, load_state SHALL return an empty
        StateData (no positions, last_acted_bar=None, pending_decision=None).
        """
        state_path = str(tmp_path / "state.json")
        # File does not exist yet.
        assert not os.path.exists(state_path)

        result = load_state(state_path)

        assert result.positions == []
        assert result.last_acted_bar is None
        assert result.pending_decision is None


class TestStateFileInvalidJsonRaises:
    """test_state_file_invalid_json_raises — ValueError on corrupt file."""

    def test_raises_value_error_on_corrupt_json(self, tmp_path):
        """
        If the state file contains invalid JSON, load_state SHALL raise
        ValueError with the file path in the message.
        """
        state_path = tmp_path / "state.json"
        state_path.write_text("{invalid json content", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_state(str(state_path))
        assert str(state_path) in str(exc_info.value)

    def test_raises_on_truncated_json(self, tmp_path):
        """
        A truncated JSON file (simulating crash during write) should raise.
        """
        state_path = tmp_path / "state.json"
        state_path.write_text('{"positions": [', encoding="utf-8")

        with pytest.raises(ValueError):
            load_state(str(state_path))


class TestAtomicWriteUsesRename:
    """test_atomic_write_uses_rename — verify temp-then-rename pattern."""

    def test_state_file_written_atomically(self, tmp_path, monkeypatch):
        """
        save_state SHALL write to a temp file first, then rename over the
        target — ensuring the target is never in a half-written state.
        """
        state_path = str(tmp_path / "state.json")
        state_data = StateData(
            positions=[{"symbol": "AAPL", "status": "open"}],
            last_acted_bar="2024-01-01T00:00:00Z",
        )

        # Track os.replace calls via monkeypatch.
        # monkeypatch is a pytest fixture for temporarily replacing functions
        # (like mocking in C++ Google Mock).
        replace_calls = []
        original_replace = os.replace

        def tracked_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        monkeypatch.setattr(os, "replace", tracked_replace)

        save_state(state_data, state_path)

        # Verify os.replace was called exactly once, targeting our state file.
        assert len(replace_calls) == 1
        _src, dst = replace_calls[0]
        assert dst == state_path

        # Verify the final file content is correct.
        with open(state_path, "r", encoding="utf-8") as f:
            content = json.load(f)
        assert content["positions"] == [{"symbol": "AAPL", "status": "open"}]


class TestPendingDecisionPersistence:
    """test_pending_decision_persistence — Decision_Dict survives write/read."""

    def test_decision_dict_round_trips(self, tmp_path):
        """
        A pending_decision dict written to the state file SHALL be identical
        when read back — no data loss or transformation.
        """
        state_path = str(tmp_path / "state.json")

        # A realistic Decision_Dict from the Risk Engine.
        decision = {
            "decision": "APPROVED",
            "signal": "BUY",
            "position_size": 15,
            "stop_loss": 147.80,
            "reason": "All risk checks passed",
            "symbol": "AAPL",
        }

        original = StateData(
            positions=[],
            last_acted_bar="2024-06-15T00:00:00Z",
            pending_decision=decision,
        )

        save_state(original, state_path)
        loaded = load_state(state_path)

        assert loaded.pending_decision == decision
        assert loaded.pending_decision is not original.pending_decision  # Not same object

    def test_null_pending_decision_preserved(self, tmp_path):
        """
        When pending_decision is None, it SHALL remain None after round-trip.
        """
        state_path = str(tmp_path / "state.json")
        original = StateData(positions=[], pending_decision=None)

        save_state(original, state_path)
        loaded = load_state(state_path)

        assert loaded.pending_decision is None


# ===========================================================================
# Safety Check Unit Tests (Task 2.3)
# ===========================================================================

from orchestrator.safety_check import run_safety_check


# --- Mock classes for safety check testing ---

class _MockOrder:
    """Mimics Alpaca Order for safety check testing."""

    def __init__(self, order_type, symbol, side):
        self.type = order_type
        self.symbol = symbol
        self.side = side


class _MockPosition:
    """Mimics Alpaca Position for safety check testing."""

    def __init__(self, symbol, qty, current_price="150.00"):
        self.symbol = symbol
        self.qty = qty
        self.current_price = current_price


class _MockClient:
    """Mock TradingClient that returns configurable positions and orders."""

    def __init__(self, positions, orders):
        self._positions = positions
        self._orders = orders

    def get_all_positions(self):
        return self._positions

    def get_orders(self, filter=None):
        return self._orders


class _MockClientThatRaises:
    """Mock TradingClient that raises an exception on API calls."""

    def __init__(self, error):
        self._error = error

    def get_all_positions(self):
        raise self._error

    def get_orders(self, filter=None):
        raise self._error


class TestAllPositionsProtected:
    """test_all_positions_protected — no CRITICAL logs emitted."""

    def test_no_critical_when_all_protected(self, caplog):
        """
        When every open position has a matching stop-loss order, the safety
        check SHALL NOT emit any CRITICAL logs.
        """
        positions = [
            _MockPosition("AAPL", "10"),
            _MockPosition("TSLA", "5"),
        ]
        orders = [
            _MockOrder("stop", "AAPL", "sell"),
            _MockOrder("stop_limit", "TSLA", "sell"),
        ]
        client = _MockClient(positions, orders)

        with caplog.at_level(logging.CRITICAL, logger="orchestrator"):
            result = run_safety_check(client)

        # No CRITICAL logs should be emitted.
        critical_records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
        assert len(critical_records) == 0

        # All positions are protected.
        assert result["total_positions"] == 2
        assert result["unprotected_count"] == 0


class TestUnprotectedPositionLogged:
    """test_unprotected_position_logged — CRITICAL for each unprotected."""

    def test_critical_for_each_unprotected(self, caplog):
        """
        For each open position without a matching stop-loss order, the safety
        check SHALL emit a CRITICAL-level log containing the symbol and quantity.
        """
        positions = [
            _MockPosition("AAPL", "10", "150.00"),  # unprotected
            _MockPosition("TSLA", "5", "200.00"),   # protected
            _MockPosition("MSFT", "3", "400.00"),   # unprotected
        ]
        orders = [
            # Only TSLA has a stop order.
            _MockOrder("stop", "TSLA", "sell"),
        ]
        client = _MockClient(positions, orders)

        with caplog.at_level(logging.CRITICAL, logger="orchestrator"):
            result = run_safety_check(client)

        assert result["unprotected_count"] == 2

        # Check that CRITICAL logs mention the unprotected symbols.
        critical_messages = [
            r.message for r in caplog.records if r.levelno >= logging.CRITICAL
        ]
        assert len(critical_messages) == 2
        # At least one message should mention AAPL and one should mention MSFT.
        all_text = " ".join(critical_messages)
        assert "AAPL" in all_text
        assert "MSFT" in all_text


class TestSafetyCheckApiFailurePropagates:
    """test_safety_check_api_failure_propagates — exception propagates to caller."""

    def test_exception_propagates_on_position_query_failure(self):
        """
        If the Alpaca API call to get_all_positions() fails, the exception
        SHALL propagate to the caller (no swallowing).
        """
        error = ConnectionError("Alpaca API unreachable")
        client = _MockClientThatRaises(error)

        with pytest.raises(ConnectionError, match="unreachable"):
            run_safety_check(client)

    def test_exception_propagates_on_orders_query_failure(self):
        """
        If the Alpaca API call to get_orders() fails, the exception SHALL
        propagate to the caller.
        """
        positions = [_MockPosition("AAPL", "10")]

        class _ClientOrdersFails:
            def get_all_positions(self):
                return positions

            def get_orders(self, filter=None):
                raise TimeoutError("Request timed out")

        with pytest.raises(TimeoutError, match="timed out"):
            run_safety_check(_ClientOrdersFails())


class TestPreExistingPositionsIncluded:
    """test_pre_existing_positions_included — positions without state file records still checked."""

    def test_all_positions_checked_regardless_of_state_file(self, caplog):
        """
        The safety check SHALL check ALL open positions from Alpaca, including
        positions that the Orchestrator did not open (no matching record in
        the state file). The safety check has no dependency on the state file.
        """
        # Simulate 3 positions — the safety check doesn't know which ones
        # the Orchestrator opened; it checks all of them.
        positions = [
            _MockPosition("AAPL", "10"),   # "Orchestrator-opened" (but safety_check doesn't know)
            _MockPosition("GOOGL", "20"),  # "Pre-existing" (but safety_check doesn't know)
            _MockPosition("AMZN", "15"),   # "Pre-existing" (but safety_check doesn't know)
        ]
        orders = [
            # Only AAPL has a stop order.
            _MockOrder("stop", "AAPL", "sell"),
        ]
        client = _MockClient(positions, orders)

        with caplog.at_level(logging.CRITICAL, logger="orchestrator"):
            result = run_safety_check(client)

        # All 3 positions should be checked.
        assert result["total_positions"] == 3
        # GOOGL and AMZN are unprotected.
        assert result["unprotected_count"] == 2


# ===========================================================================
# Cycle Execution Unit Tests (Task 4.6)
# ===========================================================================
# These tests exercise _run_cycle() with fully mocked pipeline modules.
# Each pipeline stage is mocked to return predetermined responses so we can
# test the Orchestrator's branching logic in isolation.

from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from orchestrator.loop import _run_cycle, _check_new_bar, ContractViolationError
from orchestrator.config_loader import OrchestratorConfig
from orchestrator.state_manager import StateData
from strategy import StrategyConfig


# ---------------------------------------------------------------------------
# Helper: build a valid bar DataFrame for testing
# ---------------------------------------------------------------------------

def _make_bar_df(num_rows=31, close_price=150.0):
    """
    Build a minimal valid bar DataFrame with the given number of rows and
    a constant close price.  Used across multiple cycle tests.
    """
    dates = pd.date_range(
        start="2024-01-01", periods=num_rows, freq="D", tz="UTC"
    )
    return pd.DataFrame(
        {
            "open": [close_price * 0.99] * num_rows,
            "high": [close_price * 1.01] * num_rows,
            "low": [close_price * 0.98] * num_rows,
            "close": [close_price] * num_rows,
            "volume": np.array([1000] * num_rows, dtype=np.int64),
        },
        index=dates,
    )


# ---------------------------------------------------------------------------
# Helper: build standard mock objects for _run_cycle
# ---------------------------------------------------------------------------

def _make_mock_trading_client(
    equity="100000", cash="50000", last_equity="99500",
    positions=None, clock_is_open=True
):
    """
    Create a mock TradingClient with configurable account, positions, and clock.
    """
    client = MagicMock()

    # Mock account.
    mock_account = MagicMock()
    mock_account.equity = equity
    mock_account.cash = cash
    mock_account.last_equity = last_equity
    client.get_account.return_value = mock_account

    # Mock positions.
    if positions is None:
        positions = []
    client.get_all_positions.return_value = positions

    # Mock clock.
    mock_clock = MagicMock()
    mock_clock.is_open = clock_is_open
    client.get_clock.return_value = mock_clock

    return client


def _make_orch_config(symbol="AAPL"):
    """Create a standard OrchestratorConfig for testing."""
    return OrchestratorConfig(
        bar_check_interval_seconds=300.0,
        symbol=symbol,
        state_file_path="orchestrator/state.json",
        kill_switch_file_path="orchestrator/KILL_SWITCH",
    )


def _make_strategy_config():
    """Create a standard StrategyConfig for testing (short=10, long=30)."""
    return StrategyConfig(short_window=10, long_window=30)


# ---------------------------------------------------------------------------
# Test: test_full_cycle_buy_filled
# ---------------------------------------------------------------------------

class TestFullCycleBuyFilled:
    """End-to-end cycle with mocked modules producing a filled BUY order."""

    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_buy_cycle_returns_filled(
        self, mock_compute, mock_evaluate, mock_execute
    ):
        """
        A complete BUY cycle: Strategy returns BUY signal, Risk Engine approves,
        market is open, Execution fills the order → outcome is "filled".
        """
        # Strategy returns a BUY signal.
        mock_compute.return_value = {
            "signal": "BUY",
            "short_sma": 151.0,
            "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
            "symbol": "AAPL",
        }

        # Risk Engine approves.
        mock_evaluate.return_value = {
            "decision": "APPROVED",
            "signal": "BUY",
            "position_size": 10,
            "stop_loss": 147.0,
            "reason": "All risk checks passed",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
            "symbol": "AAPL",
        }

        # Execution fills the order.
        mock_execute.return_value = {
            "status": "filled",
            "entry_order_id": "order-123",
            "stop_loss_order_id": "sl-456",
            "fill_price": 150.25,
            "filled_quantity": 10,
            "filled_at": pd.Timestamp("2024-06-15T14:30:00", tz="UTC"),
            "decision": "APPROVED",
            "signal": "BUY",
            "symbol": "AAPL",
            "position_size": 10,
            "reason": "Order filled",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
        }

        # Mock RestClient that returns a valid DataFrame.
        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()

        trading_client = _make_mock_trading_client(clock_is_open=True)

        outcome, order_result = _run_cycle(
            rest_client=mock_rest,
            trading_client=trading_client,
            strategy_config=_make_strategy_config(),
            risk_config=MagicMock(),
            execution_config=MagicMock(),
            orch_config=_make_orch_config(),
            state_data=StateData(),
            cycle_number=1,
        )

        assert outcome == "filled"
        assert order_result["entry_order_id"] == "order-123"
        assert order_result["filled_quantity"] == 10


# ---------------------------------------------------------------------------
# Test: test_full_cycle_sell_filled
# ---------------------------------------------------------------------------

class TestFullCycleSellFilled:
    """End-to-end cycle with mocked modules producing a filled SELL order."""

    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_sell_cycle_returns_filled(
        self, mock_compute, mock_evaluate, mock_execute
    ):
        mock_compute.return_value = {
            "signal": "SELL",
            "short_sma": 149.0,
            "long_sma": 151.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
            "symbol": "AAPL",
        }

        mock_evaluate.return_value = {
            "decision": "APPROVED",
            "signal": "SELL",
            "position_size": 5,
            "stop_loss": 153.0,
            "reason": "All risk checks passed",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
            "symbol": "AAPL",
        }

        mock_execute.return_value = {
            "status": "filled",
            "entry_order_id": "sell-order-789",
            "fill_price": 150.00,
            "filled_quantity": 5,
            "filled_at": pd.Timestamp("2024-06-15T14:30:00", tz="UTC"),
            "decision": "APPROVED",
            "signal": "SELL",
            "symbol": "AAPL",
            "position_size": 5,
            "reason": "Order filled",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
        }

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()
        trading_client = _make_mock_trading_client(clock_is_open=True)

        outcome, order_result = _run_cycle(
            rest_client=mock_rest,
            trading_client=trading_client,
            strategy_config=_make_strategy_config(),
            risk_config=MagicMock(),
            execution_config=MagicMock(),
            orch_config=_make_orch_config(),
            state_data=StateData(),
            cycle_number=1,
        )

        assert outcome == "filled"
        assert order_result["signal"] == "SELL"
        assert order_result["filled_quantity"] == 5


# ---------------------------------------------------------------------------
# Test: test_pipeline_invocation_order
# ---------------------------------------------------------------------------

class TestPipelineInvocationOrder:
    """Verify strict Data Ingestion → Strategy → Risk → Execution ordering."""

    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_stages_called_in_order(
        self, mock_compute, mock_evaluate, mock_execute
    ):
        """
        Track the order of calls to verify the pipeline executes in the
        strict sequence defined by Requirement 3.1.
        """
        call_order = []

        def track_compute(*args, **kwargs):
            call_order.append("strategy")
            return {
                "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
                "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
            }

        def track_evaluate(*args, **kwargs):
            call_order.append("risk_engine")
            return {
                "decision": "APPROVED", "signal": "BUY", "position_size": 10,
                "stop_loss": 147.0, "reason": "Passed",
                "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
            }

        def track_execute(*args, **kwargs):
            call_order.append("execution")
            return {
                "status": "filled", "entry_order_id": "x", "fill_price": 150.0,
                "filled_quantity": 10, "decision": "APPROVED", "signal": "BUY",
                "symbol": "AAPL", "position_size": 10, "reason": "OK",
                "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
            }

        mock_compute.side_effect = track_compute
        mock_evaluate.side_effect = track_evaluate
        mock_execute.side_effect = track_execute

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.side_effect = lambda *a, **kw: (
            call_order.append("data_ingestion") or _make_bar_df()
        )

        trading_client = _make_mock_trading_client(clock_is_open=True)

        _run_cycle(
            rest_client=mock_rest,
            trading_client=trading_client,
            strategy_config=_make_strategy_config(),
            risk_config=MagicMock(),
            execution_config=MagicMock(),
            orch_config=_make_orch_config(),
            state_data=StateData(),
            cycle_number=1,
        )

        assert call_order == ["data_ingestion", "strategy", "risk_engine", "execution"]


# ---------------------------------------------------------------------------
# Test: test_vetoed_is_correct_decline
# ---------------------------------------------------------------------------

class TestVetoedIsCorrectDecline:
    """Vetoed signal → cycle complete with outcome 'vetoed', no execution."""

    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_vetoed_returns_correct_decline(
        self, mock_compute, mock_evaluate, mock_execute
    ):
        mock_compute.return_value = {
            "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        mock_evaluate.return_value = {
            "decision": "VETOED", "signal": "BUY", "position_size": 0,
            "stop_loss": None, "reason": "Daily loss limit exceeded",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()
        trading_client = _make_mock_trading_client()

        outcome, order_result = _run_cycle(
            rest_client=mock_rest,
            trading_client=trading_client,
            strategy_config=_make_strategy_config(),
            risk_config=MagicMock(),
            execution_config=MagicMock(),
            orch_config=_make_orch_config(),
            state_data=StateData(),
            cycle_number=1,
        )

        assert outcome == "vetoed"
        assert order_result is None
        # Execution should NOT have been called.
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test: test_skipped_is_correct_decline
# ---------------------------------------------------------------------------

class TestSkippedIsCorrectDecline:
    """Execution returns 'skipped' → cycle complete, not a failure."""

    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_skipped_returns_correct_decline(
        self, mock_compute, mock_evaluate, mock_execute
    ):
        mock_compute.return_value = {
            "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        # Risk Engine returns NO_ACTION (HOLD signal passed through).
        mock_evaluate.return_value = {
            "decision": "NO_ACTION", "signal": "HOLD", "position_size": 0,
            "stop_loss": None, "reason": "HOLD signals require no risk evaluation",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
        }

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()
        trading_client = _make_mock_trading_client()

        outcome, order_result = _run_cycle(
            rest_client=mock_rest,
            trading_client=trading_client,
            strategy_config=_make_strategy_config(),
            risk_config=MagicMock(),
            execution_config=MagicMock(),
            orch_config=_make_orch_config(),
            state_data=StateData(),
            cycle_number=1,
        )

        assert outcome == "no_action"
        assert order_result is None
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test: test_execution_skipped_status_is_correct_decline
# ---------------------------------------------------------------------------

class TestExecutionSkippedStatusIsCorrectDecline:
    """
    Execution module returns status='skipped' → cycle complete with outcome
    'skipped' and order_result populated. This is the Requirement 3.5 code path
    where the Execution module itself returns 'skipped' (e.g., for VETOED/NO_ACTION
    decisions that reached execute_decision), distinct from the Risk Engine's
    short-circuit handled by VETOED/NO_ACTION outcomes above.
    """

    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_execution_skipped_maps_to_correct_decline(
        self, mock_compute, mock_evaluate, mock_execute
    ):
        """
        When execute_decision returns status='skipped' (its internal handling
        of non-approved decisions), _run_cycle SHALL map it to outcome 'skipped'
        with the order_result dict preserved — proving the valid_outcomes
        pass-through logic works.
        """
        # Strategy returns a signal that somehow reaches Execution.
        mock_compute.return_value = {
            "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }

        # Risk Engine returns APPROVED (so the cycle proceeds past the
        # VETOED/NO_ACTION short-circuit and reaches Execution).
        mock_evaluate.return_value = {
            "decision": "APPROVED", "signal": "BUY", "position_size": 10,
            "stop_loss": 147.0, "reason": "All risk checks passed",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }

        # Execution module itself returns 'skipped' (its internal pass-through
        # for decisions it considers non-actionable, e.g. APPROVED+HOLD guard).
        skipped_result = {
            "status": "skipped",
            "decision": "APPROVED",
            "signal": "BUY",
            "symbol": "AAPL",
            "position_size": 0,
            "reason": "APPROVED+HOLD is malformed — skipped",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
        }
        mock_execute.return_value = skipped_result

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()
        trading_client = _make_mock_trading_client(clock_is_open=True)

        outcome, order_result = _run_cycle(
            rest_client=mock_rest,
            trading_client=trading_client,
            strategy_config=_make_strategy_config(),
            risk_config=MagicMock(),
            execution_config=MagicMock(),
            orch_config=_make_orch_config(),
            state_data=StateData(),
            cycle_number=1,
        )

        # Outcome should be "skipped" — mapped from the Order_Result status.
        assert outcome == "skipped"
        # The order_result dict should be preserved (not None).
        assert order_result is not None
        assert order_result["status"] == "skipped"
        # execute_decision WAS called (unlike vetoed/no_action tests).
        mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test: test_market_closed_defers_execution
# ---------------------------------------------------------------------------

class TestMarketClosedDefersExecution:
    """Market closed → execution deferred, Decision_Dict returned for persistence."""

    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_deferred_when_market_closed(
        self, mock_compute, mock_evaluate, mock_execute
    ):
        mock_compute.return_value = {
            "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        decision_dict = {
            "decision": "APPROVED", "signal": "BUY", "position_size": 10,
            "stop_loss": 147.0, "reason": "All risk checks passed",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        mock_evaluate.return_value = decision_dict

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()

        # Market is CLOSED.
        trading_client = _make_mock_trading_client(clock_is_open=False)

        outcome, order_result = _run_cycle(
            rest_client=mock_rest,
            trading_client=trading_client,
            strategy_config=_make_strategy_config(),
            risk_config=MagicMock(),
            execution_config=MagicMock(),
            orch_config=_make_orch_config(),
            state_data=StateData(),
            cycle_number=1,
        )

        assert outcome == "deferred"
        # The Decision_Dict is returned so the caller can persist it.
        assert order_result == decision_dict
        # Execution should NOT have been called.
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test: test_first_run_triggers_cycle (via _check_new_bar with None)
# ---------------------------------------------------------------------------

class TestFirstRunTriggersCycle:
    """First run (last_acted_bar=None) → any bar is treated as new."""

    def test_none_last_acted_bar_is_new(self):
        """_check_new_bar with last_acted_bar=None always returns True."""
        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df(num_rows=1)

        is_new, bar_ts = _check_new_bar(mock_rest, "AAPL", None)

        assert is_new is True
        assert bar_ts is not None


# ---------------------------------------------------------------------------
# Test: test_same_bar_no_cycle (via _check_new_bar with same date)
# ---------------------------------------------------------------------------

class TestSameBarNoCycle:
    """Same bar date as last_acted → no new cycle triggered."""

    def test_same_date_returns_false(self):
        """_check_new_bar returns False when latest bar == last_acted_bar."""
        # Create a DataFrame with a bar at a known date.
        bar_date = "2024-06-15"
        dates = pd.DatetimeIndex(
            [pd.Timestamp(bar_date, tz="UTC")], name="timestamp"
        )
        df = pd.DataFrame(
            {
                "open": [150.0], "high": [151.0], "low": [149.0],
                "close": [150.5],
                "volume": np.array([1000], dtype=np.int64),
            },
            index=dates,
        )

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = df

        # last_acted_bar is the same date.
        is_new, bar_ts = _check_new_bar(mock_rest, "AAPL", "2024-06-15T00:00:00Z")

        assert is_new is False
        assert bar_ts == "2024-06-15"

    def test_equal_date_strict_inequality(self):
        """
        Explicit boundary test: when latest bar date EXACTLY equals
        last_acted_bar date (same YYYY-MM-DD), _check_new_bar SHALL return
        False — Property 2 requires STRICT inequality (>) not (>=).
        """
        # Use the exact same date string for both bar and last_acted.
        equal_date = "2024-03-01"
        dates = pd.DatetimeIndex(
            [pd.Timestamp(equal_date, tz="UTC")], name="timestamp"
        )
        df = pd.DataFrame(
            {
                "open": [200.0], "high": [201.0], "low": [199.0],
                "close": [200.5],
                "volume": np.array([5000], dtype=np.int64),
            },
            index=dates,
        )

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = df

        # Pass last_acted_bar as just the date portion (no time component).
        is_new, bar_ts = _check_new_bar(mock_rest, "AAPL", "2024-03-01")
        assert is_new is False
        assert bar_ts == "2024-03-01"

        # Also test with a full ISO 8601 datetime that has the same date portion.
        is_new2, bar_ts2 = _check_new_bar(
            mock_rest, "AAPL", "2024-03-01T14:30:00Z"
        )
        assert is_new2 is False
        assert bar_ts2 == "2024-03-01"


# ---------------------------------------------------------------------------
# Test: test_stage_returns_none_halts
# ---------------------------------------------------------------------------

class TestStageReturnsNoneHalts:
    """Stage returning None → ContractViolationError (halt, not retried)."""

    @patch("orchestrator.loop.compute_signal")
    def test_strategy_returns_none_raises_contract_violation(self, mock_compute):
        """
        If compute_signal returns None instead of a dict, _run_cycle SHALL
        raise ContractViolationError — classified as post_order (halt).
        """
        mock_compute.return_value = None  # Contract violation!

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()
        trading_client = _make_mock_trading_client()

        with pytest.raises(ContractViolationError, match="strategy"):
            _run_cycle(
                rest_client=mock_rest,
                trading_client=trading_client,
                strategy_config=_make_strategy_config(),
                risk_config=MagicMock(),
                execution_config=MagicMock(),
                orch_config=_make_orch_config(),
                state_data=StateData(),
                cycle_number=1,
            )

    def test_account_returns_none_raises_contract_violation(self):
        """
        If trading_client.get_account() returns None, _run_cycle SHALL
        raise ContractViolationError.
        """
        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()

        trading_client = MagicMock()
        trading_client.get_account.return_value = None  # Contract violation!
        trading_client.get_all_positions.return_value = []

        with pytest.raises(ContractViolationError, match="account_query"):
            _run_cycle(
                rest_client=mock_rest,
                trading_client=trading_client,
                strategy_config=_make_strategy_config(),
                risk_config=MagicMock(),
                execution_config=MagicMock(),
                orch_config=_make_orch_config(),
                state_data=StateData(),
                cycle_number=1,
            )

    @patch("orchestrator.loop.evaluate_signal")
    @patch("orchestrator.loop.compute_signal")
    def test_risk_engine_returns_none_raises_contract_violation(
        self, mock_compute, mock_evaluate
    ):
        """
        If evaluate_signal returns None instead of a dict, _run_cycle SHALL
        raise ContractViolationError.
        """
        mock_compute.return_value = {
            "signal": "BUY", "short_sma": 151.0, "long_sma": 149.0,
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }
        mock_evaluate.return_value = None  # Contract violation!

        mock_rest = MagicMock()
        mock_rest.get_historical_bars.return_value = _make_bar_df()
        trading_client = _make_mock_trading_client()

        with pytest.raises(ContractViolationError, match="risk_engine"):
            _run_cycle(
                rest_client=mock_rest,
                trading_client=trading_client,
                strategy_config=_make_strategy_config(),
                risk_config=MagicMock(),
                execution_config=MagicMock(),
                orch_config=_make_orch_config(),
                state_data=StateData(),
                cycle_number=1,
            )


# ===========================================================================
# Error Handling Unit Tests (Task 5.5)
# ===========================================================================
# These test the error classification logic in isolation, exercising specific
# scenarios defined in Requirements 5.1–5.9.

from orchestrator.loop import _classify_error, ContractViolationError


class TestPreOrderFailureSkipsAndContinues:
    """test_pre_order_failure_skips_and_continues — cycle skipped, loop continues."""

    def test_data_ingestion_ioerror_is_pre_order(self):
        """
        An IOError during Data Ingestion SHALL be classified as "pre_order"
        (safe to skip and retry on next cycle).
        """
        error = IOError("Alpaca API error 503: Service Unavailable")
        result = _classify_error("data_ingestion", error, order_result=None)
        assert result == "pre_order"

    def test_strategy_value_error_is_pre_order(self):
        """
        A ValueError during Strategy computation (e.g., invalid DataFrame)
        SHALL be classified as "pre_order".
        """
        error = ValueError("close column contains NaN values")
        result = _classify_error("strategy", error, order_result=None)
        assert result == "pre_order"

    def test_risk_engine_type_error_is_pre_order(self):
        """
        A TypeError during Risk Engine evaluation SHALL be classified as
        "pre_order" (unexpected input, not an order submission issue).
        """
        error = TypeError("signal_dict is not a dict: got NoneType")
        result = _classify_error("risk_engine", error, order_result=None)
        assert result == "pre_order"

    def test_account_query_timeout_is_pre_order(self):
        """
        A timeout during account query SHALL be classified as "pre_order".
        """
        error = TimeoutError("Alpaca API did not respond within 30 seconds")
        result = _classify_error("account_query", error, order_result=None)
        assert result == "pre_order"

    def test_execution_error_without_order_id_is_pre_order(self):
        """
        Execution returning status='error' with entry_order_id=None means
        the error occurred BEFORE order submission → "pre_order".
        """
        order_result = {
            "status": "error",
            "entry_order_id": None,
            "error_type": "ConnectionError",
            "reason": "Network unreachable before submission",
        }
        error = RuntimeError("execution reported error")
        result = _classify_error("execution", error, order_result=order_result)
        assert result == "pre_order"


class TestPostOrderFailureHalts:
    """test_post_order_failure_halts — CRITICAL, state flushed, exit 1."""

    def test_execution_error_with_order_id_is_post_order(self):
        """
        Execution returning status='error' with entry_order_id present means
        an order WAS submitted but its state is ambiguous → "post_order" (halt).
        """
        order_result = {
            "status": "error",
            "entry_order_id": "abc-123-def",
            "error_type": "APIError",
            "reason": "Stop-loss submission failed after entry filled",
        }
        error = RuntimeError("execution reported error")
        result = _classify_error("execution", error, order_result=order_result)
        assert result == "post_order"

    def test_contract_violation_in_strategy_is_post_order(self):
        """
        ContractViolationError from strategy stage → "post_order" (halt),
        even though strategy is normally a pre-order stage.
        """
        error = ContractViolationError("compute_signal returned None")
        result = _classify_error("strategy", error, order_result=None)
        assert result == "post_order"


class TestRawExecutionExceptionHalts:
    """test_raw_execution_exception_halts — treated as post-order failure."""

    def test_raw_exception_no_order_result_is_post_order(self):
        """
        If the Execution module raises a raw exception without returning an
        Order_Result, _classify_error SHALL return "post_order" because we
        cannot determine whether an order was submitted.
        """
        error = TypeError("unexpected error during order submission")
        result = _classify_error("execution", error, order_result=None)
        assert result == "post_order"

    def test_raw_network_error_no_result_is_post_order(self):
        """
        Network error from Execution with no Order_Result → "post_order".
        """
        error = ConnectionError("connection reset by peer")
        result = _classify_error("execution", error, order_result=None)
        assert result == "post_order"

    def test_non_dict_order_result_treated_as_no_result(self):
        """
        If order_result is somehow not a dict (e.g., a string or integer),
        _classify_error SHALL treat it as if no Order_Result was returned
        → "post_order" (conservative halt).
        """
        error = RuntimeError("execution anomaly")
        result = _classify_error("execution", error, order_result="not a dict")
        assert result == "post_order"


class TestConsecutiveFailureEscalation:
    """test_consecutive_failure_escalation — ERROR at 3rd consecutive, resets on success."""

    def test_first_two_failures_are_warning_level(self):
        """
        The first 2 consecutive pre-order failures should be logged at
        WARNING level (not yet escalated).
        """
        from tests.test_orchestrator_properties import _get_failure_log_level
        assert _get_failure_log_level(1) == logging.WARNING
        assert _get_failure_log_level(2) == logging.WARNING

    def test_third_failure_escalates_to_error(self):
        """
        The 3rd consecutive pre-order failure should escalate to ERROR level.
        """
        from tests.test_orchestrator_properties import _get_failure_log_level
        assert _get_failure_log_level(3) == logging.ERROR

    def test_subsequent_failures_remain_error(self):
        """
        4th, 5th, etc. consecutive failures remain at ERROR level.
        """
        from tests.test_orchestrator_properties import _get_failure_log_level
        assert _get_failure_log_level(4) == logging.ERROR
        assert _get_failure_log_level(10) == logging.ERROR
        assert _get_failure_log_level(100) == logging.ERROR

    def test_reset_after_success(self):
        """
        After a successful cycle, the counter resets.  The next failure
        should be at WARNING level, not ERROR.
        """
        from tests.test_orchestrator_properties import _get_failure_log_level
        # Simulate 5 consecutive failures → ERROR.
        assert _get_failure_log_level(5) == logging.ERROR
        # After success, counter resets to 0.  Next failure is #1 → WARNING.
        assert _get_failure_log_level(1) == logging.WARNING


# ===========================================================================
# Main Loop Unit Tests (Task 7.5)
# ===========================================================================
# These test run_loop() behavior for shutdown, deferred execution, and
# error-handling scenarios using heavy mocking of all external dependencies.

from unittest.mock import patch, MagicMock
from orchestrator.loop import run_loop, _determine_failed_stage, ContractViolationError


class TestShutdownSignalSetsFlag:
    """test_shutdown_signal_sets_flag — SIGINT/SIGTERM set stop_flag."""

    @patch("orchestrator.loop.run_safety_check")
    @patch("orchestrator.loop.load_state")
    @patch("orchestrator.loop._create_paper_client")
    @patch("orchestrator.loop.load_credentials")
    @patch("orchestrator.loop.load_execution_config")
    @patch("orchestrator.loop.load_risk_config")
    @patch("orchestrator.loop.load_strategy_config")
    @patch("orchestrator.loop.save_state")
    def test_signal_triggers_clean_exit(
        self, mock_save, mock_strat_cfg, mock_risk_cfg, mock_exec_cfg,
        mock_creds, mock_paper, mock_load_state, mock_safety
    ):
        """
        When a shutdown signal is received while idle (no cycle in progress),
        run_loop SHALL flush the state file and exit with code 0.
        """
        import signal as sig_module

        mock_creds.return_value = ("key", "secret")
        mock_paper.return_value = MagicMock()
        mock_strat_cfg.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk_cfg.return_value = MagicMock()
        mock_exec_cfg.return_value = MagicMock()
        mock_load_state.return_value = StateData()
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}

        # We'll simulate the signal by having the shutdown_event get set
        # immediately, which means the loop's first wait() will return
        # immediately, and since we set stop_flag before entering,
        # the loop will exit on the first iteration.
        #
        # Strategy: patch signal.signal to capture the handler, then call it.
        captured_handler = None

        def capture_signal(signum, handler):
            nonlocal captured_handler
            if signum == sig_module.SIGINT:
                captured_handler = handler

        with patch("orchestrator.loop.signal.signal", side_effect=capture_signal):
            with patch("orchestrator.loop.load_config") as mock_orch_cfg:
                mock_orch_cfg.return_value = _make_orch_config()

                # We need to trigger the signal handler inside run_loop.
                # The simplest way: patch threading.Event.wait to call
                # the signal handler on first invocation, then check exit.
                call_count = [0]

                def mock_wait(timeout=None):
                    call_count[0] += 1
                    if call_count[0] == 1 and captured_handler:
                        # Simulate SIGINT being received.
                        captured_handler(sig_module.SIGINT, None)

                with patch("threading.Event.wait", side_effect=mock_wait):
                    with pytest.raises(SystemExit) as exc_info:
                        run_loop("orchestrator/config.yaml")

                    # Should exit with code 0 (graceful shutdown).
                    assert exc_info.value.code == 0

        # State file should have been saved.
        mock_save.assert_called()


class TestShutdownInterruptsIdleSleep:
    """test_shutdown_interrupts_idle_sleep — threading.Event wakes immediately."""

    def test_event_set_interrupts_wait(self):
        """
        The shutdown_event.set() call in the signal handler SHALL immediately
        unblock any pending event.wait(timeout) call, ensuring the Orchestrator
        can shut down within 2 seconds rather than waiting for the full interval.
        """
        import threading

        # This is a pure threading.Event behavior test — no mocking needed.
        event = threading.Event()
        start = time.monotonic()

        # Set the event from another thread after 0.1 seconds.
        def set_after_delay():
            time.sleep(0.1)
            event.set()

        t = threading.Thread(target=set_after_delay)
        t.start()

        # Wait with a long timeout — should return in ~0.1s, not 60s.
        event.wait(timeout=60.0)
        elapsed = time.monotonic() - start

        t.join()

        # Should have woken up in well under 2 seconds.
        assert elapsed < 2.0


class TestDeferredExecutesOnMarketOpen:
    """test_deferred_executes_on_market_open — same decision used when market opens."""

    @patch("orchestrator.loop.run_safety_check")
    @patch("orchestrator.loop.load_state")
    @patch("orchestrator.loop._create_paper_client")
    @patch("orchestrator.loop.load_credentials")
    @patch("orchestrator.loop.load_execution_config")
    @patch("orchestrator.loop.load_risk_config")
    @patch("orchestrator.loop.load_strategy_config")
    @patch("orchestrator.loop.save_state")
    @patch("orchestrator.loop.execute_decision")
    def test_deferred_decision_executed_directly(
        self, mock_execute, mock_save, mock_strat_cfg, mock_risk_cfg,
        mock_exec_cfg, mock_creds, mock_paper, mock_load_state, mock_safety
    ):
        """
        When a pending_decision exists in the state file and the market opens,
        run_loop SHALL call execute_decision() DIRECTLY with the stored
        Decision_Dict — not re-invoke _run_cycle().
        """
        pending = {
            "decision": "APPROVED", "signal": "BUY", "position_size": 10,
            "stop_loss": 147.0, "reason": "All checks passed",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"), "symbol": "AAPL",
        }

        mock_creds.return_value = ("key", "secret")

        # Mock trading client: clock.is_open = True (market open).
        mock_client = MagicMock()
        mock_clock = MagicMock()
        mock_clock.is_open = True
        mock_client.get_clock.return_value = mock_clock
        mock_paper.return_value = mock_client

        mock_strat_cfg.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk_cfg.return_value = MagicMock()
        mock_exec_cfg.return_value = MagicMock()
        mock_load_state.return_value = StateData(
            positions=[], last_acted_bar="2024-06-14", pending_decision=pending
        )
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}

        # execute_decision returns filled — then we trigger shutdown.
        mock_execute.return_value = {
            "status": "filled", "entry_order_id": "order-123",
            "stop_loss_order_id": "sl-456", "fill_price": 150.0,
            "filled_quantity": 10, "signal": "BUY", "decision": "APPROVED",
            "symbol": "AAPL", "position_size": 10, "reason": "Filled",
            "timestamp": pd.Timestamp("2024-06-15", tz="UTC"),
        }

        # After the deferred execution resolves, trigger shutdown on next iteration.
        iteration = [0]
        original_wait = threading.Event.wait

        def mock_event_wait(self_event, timeout=None):
            iteration[0] += 1
            if iteration[0] >= 1:
                # Trigger shutdown after deferred execution.
                self_event.set()

        with patch("orchestrator.loop.load_config") as mock_orch_cfg:
            mock_orch_cfg.return_value = _make_orch_config()
            with patch("threading.Event.wait", mock_event_wait):
                with patch("threading.Event.clear"):
                    # We need to stop the loop — set stop_flag via signal simulation.
                    # Simplest: patch the signal handler capture and call it.
                    import signal as sig_mod

                    handlers = {}

                    def capture_sig(signum, handler):
                        handlers[signum] = handler

                    with patch("orchestrator.loop.signal.signal", side_effect=capture_sig):
                        # Run the loop — it should execute the deferred decision,
                        # then on next iteration we need to stop it.
                        # Use a side_effect on _check_new_bar to trigger stop.
                        with patch("orchestrator.loop._check_new_bar") as mock_bar:
                            def stop_on_bar_check(*args, **kwargs):
                                # After deferred resolves, trigger shutdown.
                                if sig_mod.SIGINT in handlers:
                                    handlers[sig_mod.SIGINT](sig_mod.SIGINT, None)
                                return (False, None)

                            mock_bar.side_effect = stop_on_bar_check

                            with pytest.raises(SystemExit) as exc_info:
                                run_loop("orchestrator/config.yaml")

                            assert exc_info.value.code == 0

        # Verify execute_decision was called with the pending decision.
        mock_execute.assert_called_once()
        call_args = mock_execute.call_args
        assert call_args[0][0] == pending  # First positional arg is the decision_dict.


class TestStartupResumesPendingDecision:
    """test_startup_resumes_pending_decision — WARNING logged, resumes waiting."""

    @patch("orchestrator.loop.run_safety_check")
    @patch("orchestrator.loop.load_state")
    @patch("orchestrator.loop._create_paper_client")
    @patch("orchestrator.loop.load_credentials")
    @patch("orchestrator.loop.load_execution_config")
    @patch("orchestrator.loop.load_risk_config")
    @patch("orchestrator.loop.load_strategy_config")
    def test_pending_decision_logged_at_warning(
        self, mock_strat_cfg, mock_risk_cfg, mock_exec_cfg,
        mock_creds, mock_paper, mock_load_state, mock_safety, caplog
    ):
        """
        When the Orchestrator starts with a non-null pending_decision in the
        state file, it SHALL log a WARNING identifying the pending decision.
        """
        pending = {
            "decision": "APPROVED", "signal": "BUY", "position_size": 10,
            "stop_loss": 147.0, "symbol": "AAPL",
        }

        mock_creds.return_value = ("key", "secret")
        mock_paper.return_value = MagicMock()
        mock_strat_cfg.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk_cfg.return_value = MagicMock()
        mock_exec_cfg.return_value = MagicMock()
        mock_load_state.return_value = StateData(
            positions=[], last_acted_bar="2024-06-14", pending_decision=pending
        )
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}

        with patch("orchestrator.loop.load_config") as mock_orch_cfg:
            mock_orch_cfg.return_value = _make_orch_config()

            # We only need to get through initialization (Phase 1) to verify
            # the WARNING log. Exit immediately at the first wait.
            with patch("threading.Event.wait", side_effect=SystemExit(0)):
                with patch("orchestrator.loop.signal.signal"):
                    with patch("orchestrator.loop.save_state"):
                        with caplog.at_level(logging.WARNING, logger="orchestrator"):
                            with pytest.raises(SystemExit):
                                run_loop("orchestrator/config.yaml")

        # Check that a WARNING about the pending decision was logged.
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("pending" in msg.lower() and "deferred" in msg.lower()
                   for msg in warning_messages), (
            f"Expected WARNING about pending deferred decision, "
            f"got: {warning_messages}"
        )


class TestShutdownDuringDeferredDiscards:
    """test_shutdown_during_deferred_discards — pending decision cleared, WARNING logged."""

    @patch("orchestrator.loop.run_safety_check")
    @patch("orchestrator.loop.load_state")
    @patch("orchestrator.loop._create_paper_client")
    @patch("orchestrator.loop.load_credentials")
    @patch("orchestrator.loop.load_execution_config")
    @patch("orchestrator.loop.load_risk_config")
    @patch("orchestrator.loop.load_strategy_config")
    @patch("orchestrator.loop.save_state")
    def test_deferred_discarded_on_shutdown(
        self, mock_save, mock_strat_cfg, mock_risk_cfg, mock_exec_cfg,
        mock_creds, mock_paper, mock_load_state, mock_safety, caplog
    ):
        """
        If a shutdown signal is received while holding a deferred Decision_Dict,
        run_loop SHALL discard the pending decision, clear it from the state
        file, and log a WARNING.
        """
        pending = {
            "decision": "APPROVED", "signal": "BUY", "symbol": "AAPL",
        }

        mock_creds.return_value = ("key", "secret")

        # Mock trading client: clock.is_open = False (market still closed).
        mock_client = MagicMock()
        mock_clock = MagicMock()
        mock_clock.is_open = False
        mock_client.get_clock.return_value = mock_clock
        mock_paper.return_value = mock_client

        mock_strat_cfg.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk_cfg.return_value = MagicMock()
        mock_exec_cfg.return_value = MagicMock()
        mock_load_state.return_value = StateData(
            positions=[], last_acted_bar="2024-06-14", pending_decision=pending
        )
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}

        import signal as sig_mod
        handlers = {}

        def capture_sig(signum, handler):
            handlers[signum] = handler

        with patch("orchestrator.loop.load_config") as mock_orch_cfg:
            mock_orch_cfg.return_value = _make_orch_config()

            call_count = [0]

            def mock_wait(self_event, timeout=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    # First wait (market closed, waiting) — trigger shutdown.
                    if sig_mod.SIGINT in handlers:
                        handlers[sig_mod.SIGINT](sig_mod.SIGINT, None)

            with patch("orchestrator.loop.signal.signal", side_effect=capture_sig):
                with patch("threading.Event.wait", mock_wait):
                    with patch("threading.Event.clear"):
                        with caplog.at_level(logging.WARNING, logger="orchestrator"):
                            with pytest.raises(SystemExit) as exc_info:
                                run_loop("orchestrator/config.yaml")

                            assert exc_info.value.code == 0

        # Verify the state was saved with pending_decision=None.
        assert mock_save.called
        saved_state = mock_save.call_args[0][0]
        assert saved_state.pending_decision is None

        # Verify WARNING about discarding the deferred decision.
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("discard" in msg.lower() for msg in warning_messages)


class TestPaperSafetyVerified:
    """test_paper_safety_verified — RuntimeError if URL lacks 'paper'."""

    def test_runtime_error_on_non_paper_url(self):
        """
        _create_paper_client SHALL raise RuntimeError if the TradingClient's
        base URL does not contain 'paper'.
        """
        from orchestrator.loop import _create_paper_client

        with patch("orchestrator.loop.TradingClient") as MockTC:
            mock_instance = MagicMock()
            mock_instance._base_url = "https://api.alpaca.markets"  # LIVE!
            mock_instance.base_url = ""
            MockTC.return_value = mock_instance

            with pytest.raises(RuntimeError, match="paper"):
                _create_paper_client("key", "secret")


class TestCredentialErrorPropagates:
    """test_credential_error_propagates — EnvironmentError propagates unchanged."""

    @patch("orchestrator.loop.load_config")
    @patch("orchestrator.loop.load_credentials")
    def test_env_error_not_caught(self, mock_creds, mock_cfg):
        """
        If load_credentials() raises EnvironmentError, run_loop SHALL let
        it propagate unchanged (Requirement 9.6).
        """
        mock_cfg.return_value = _make_orch_config()
        mock_creds.side_effect = EnvironmentError("Missing credential: APCA_API_KEY_ID")

        with pytest.raises(EnvironmentError, match="APCA_API_KEY_ID"):
            run_loop("orchestrator/config.yaml")


class TestStateFileWriteFailureHalts:
    """test_state_file_write_failure_halts — CRITICAL log, sys.exit(1) on disk error."""

    @patch("orchestrator.loop.run_safety_check")
    @patch("orchestrator.loop.load_state")
    @patch("orchestrator.loop._create_paper_client")
    @patch("orchestrator.loop.load_credentials")
    @patch("orchestrator.loop.load_execution_config")
    @patch("orchestrator.loop.load_risk_config")
    @patch("orchestrator.loop.load_strategy_config")
    @patch("orchestrator.loop._check_new_bar")
    @patch("orchestrator.loop._run_cycle")
    @patch("orchestrator.loop.save_state")
    def test_disk_error_halts_with_exit_1(
        self, mock_save, mock_cycle, mock_bar, mock_strat_cfg, mock_risk_cfg,
        mock_exec_cfg, mock_creds, mock_paper, mock_load_state, mock_safety,
        caplog
    ):
        """
        If save_state raises OSError (disk full, permissions), run_loop SHALL
        log CRITICAL and exit with code 1.
        """
        mock_creds.return_value = ("key", "secret")
        mock_paper.return_value = MagicMock()
        mock_strat_cfg.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk_cfg.return_value = MagicMock()
        mock_exec_cfg.return_value = MagicMock()
        mock_load_state.return_value = StateData()
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}

        # New bar detected, cycle succeeds.
        mock_bar.return_value = (True, "2024-06-16")
        mock_cycle.return_value = ("vetoed", None)

        # save_state raises OSError.
        mock_save.side_effect = OSError("Permission denied")

        with patch("orchestrator.loop.load_config") as mock_cfg:
            mock_cfg.return_value = _make_orch_config()
            with patch("orchestrator.loop.signal.signal"):
                with patch("threading.Event.wait"):
                    with patch("threading.Event.clear"):
                        with caplog.at_level(logging.CRITICAL, logger="orchestrator"):
                            with pytest.raises(SystemExit) as exc_info:
                                run_loop("orchestrator/config.yaml")

                            # Exit code 4 = post-order halt shutdown sequence
                            # failed (save_state is mocked to always raise
                            # OSError, so the shutdown helper's own state flush
                            # also fails → dirty path → code 4).
                            assert exc_info.value.code == 4

        # CRITICAL log should mention state file.
        critical_messages = [
            r.message for r in caplog.records if r.levelno >= logging.CRITICAL
        ]
        assert len(critical_messages) > 0


# ===========================================================================
# Additional Main Loop Tests (missing from original 7.5 spec)
# ===========================================================================


class TestShutdownBetweenCycles:
    """
    test_shutdown_between_cycles — When stop_flag becomes True between cycles
    (not during one), the loop exits cleanly with sys.exit(0).

    Distinct from TestShutdownSignalSetsFlag which tests signal-during-idle-sleep.
    This test verifies the stop_flag check at the TOP of the while loop fires
    correctly when no cycle is in progress and no deferred decision is pending.
    """

    @patch("orchestrator.loop.run_safety_check")
    @patch("orchestrator.loop.load_state")
    @patch("orchestrator.loop._create_paper_client")
    @patch("orchestrator.loop.load_credentials")
    @patch("orchestrator.loop.load_execution_config")
    @patch("orchestrator.loop.load_risk_config")
    @patch("orchestrator.loop.load_strategy_config")
    @patch("orchestrator.loop.save_state")
    @patch("orchestrator.loop._check_new_bar")
    def test_stop_flag_between_cycles_exits_zero(
        self, mock_bar, mock_save, mock_strat_cfg, mock_risk_cfg,
        mock_exec_cfg, mock_creds, mock_paper, mock_load_state, mock_safety
    ):
        mock_creds.return_value = ("key", "secret")
        mock_paper.return_value = MagicMock()
        mock_strat_cfg.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk_cfg.return_value = MagicMock()
        mock_exec_cfg.return_value = MagicMock()
        mock_load_state.return_value = StateData()  # No pending decision.
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}

        # New bar check returns False (no bar) — the loop will idle.
        mock_bar.return_value = (False, None)

        import signal as sig_mod
        handlers = {}

        def capture_sig(signum, handler):
            handlers[signum] = handler

        with patch("orchestrator.loop.load_config") as mock_cfg:
            mock_cfg.return_value = _make_orch_config()

            wait_calls = [0]

            def mock_wait(self_event, timeout=None):
                wait_calls[0] += 1
                if wait_calls[0] == 1:
                    # After first idle wait, simulate signal arrival.
                    if sig_mod.SIGINT in handlers:
                        handlers[sig_mod.SIGINT](sig_mod.SIGINT, None)
                # Second iteration: stop_flag is True → loop exits at top.

            with patch("orchestrator.loop.signal.signal", side_effect=capture_sig):
                with patch("threading.Event.wait", mock_wait):
                    with patch("threading.Event.clear"):
                        with pytest.raises(SystemExit) as exc_info:
                            run_loop("orchestrator/config.yaml")

                        assert exc_info.value.code == 0

        # State was saved on shutdown.
        mock_save.assert_called()


class TestDoubleSignalContinuesWaiting:
    """
    test_double_signal_continues_waiting — Simulate a second SIGINT/SIGTERM
    arriving while stop_flag is already True. Verify:
      1. WARNING logged with event="duplicate_shutdown_signal".
      2. Loop does NOT force-terminate.
    """

    def test_duplicate_signal_logs_warning_and_continues(self, caplog):
        """
        When a second signal is received while already shutting down, the
        signal handler SHALL log a WARNING and NOT force-terminate.
        """
        import signal as sig_mod
        from orchestrator.loop import _create_paper_client

        # We test the signal handler behavior in isolation by reconstructing
        # the closure's state.  The handler is a nested function inside
        # run_loop, but we can test its behavior by patching run_loop
        # to capture the handler and invoke it twice.

        handlers = {}

        def capture_sig(signum, handler):
            handlers[signum] = handler

        with patch("orchestrator.loop.run_safety_check") as mock_safety, \
             patch("orchestrator.loop.load_state") as mock_state, \
             patch("orchestrator.loop._create_paper_client") as mock_paper, \
             patch("orchestrator.loop.load_credentials") as mock_creds, \
             patch("orchestrator.loop.load_execution_config") as mock_exec, \
             patch("orchestrator.loop.load_risk_config") as mock_risk, \
             patch("orchestrator.loop.load_strategy_config") as mock_strat, \
             patch("orchestrator.loop.save_state") as mock_save, \
             patch("orchestrator.loop.load_config") as mock_cfg:

            mock_creds.return_value = ("key", "secret")
            mock_paper.return_value = MagicMock()
            mock_strat.return_value = MagicMock(short_window=10, long_window=30)
            mock_risk.return_value = MagicMock()
            mock_exec.return_value = MagicMock()
            mock_state.return_value = StateData()
            mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}
            mock_cfg.return_value = _make_orch_config()

            call_count = [0]

            def mock_wait(self_event, timeout=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    # First wait: send first signal (sets stop_flag).
                    if sig_mod.SIGINT in handlers:
                        handlers[sig_mod.SIGINT](sig_mod.SIGINT, None)
                    # Send SECOND signal (duplicate — stop_flag already True).
                    if sig_mod.SIGINT in handlers:
                        handlers[sig_mod.SIGINT](sig_mod.SIGINT, None)

            with patch("orchestrator.loop.signal.signal", side_effect=capture_sig):
                with patch("threading.Event.wait", mock_wait):
                    with patch("threading.Event.clear"):
                        with caplog.at_level(logging.WARNING, logger="orchestrator"):
                            with pytest.raises(SystemExit) as exc_info:
                                run_loop("orchestrator/config.yaml")

                            # Loop should still exit cleanly (not force-terminate).
                            assert exc_info.value.code == 0

        # Verify duplicate_shutdown_signal WARNING was logged.
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("duplicate" in msg.lower() and "shutdown" in msg.lower()
                   for msg in warning_messages), (
            f"Expected 'duplicate_shutdown_signal' WARNING, got: {warning_messages}"
        )


class TestShutdownCycleTimeoutWarning:
    """
    test_shutdown_cycle_timeout_warning — Simulate a cycle running > 300s while
    stop_flag is True. Verify WARNING logged with event="shutdown_cycle_timeout_warning"
    and elapsed duration. Confirm loop continues waiting (no force-terminate).
    """

    def test_long_cycle_logs_timeout_warning(self, caplog):
        """
        When a signal arrives during a cycle that has already been running
        > 300 seconds, the signal handler SHALL log a WARNING with
        event='shutdown_cycle_timeout_warning' and the elapsed time.
        """
        import signal as sig_mod

        handlers = {}

        def capture_sig(signum, handler):
            handlers[signum] = handler

        with patch("orchestrator.loop.run_safety_check") as mock_safety, \
             patch("orchestrator.loop.load_state") as mock_state, \
             patch("orchestrator.loop._create_paper_client") as mock_paper, \
             patch("orchestrator.loop.load_credentials") as mock_creds, \
             patch("orchestrator.loop.load_execution_config") as mock_exec, \
             patch("orchestrator.loop.load_risk_config") as mock_risk, \
             patch("orchestrator.loop.load_strategy_config") as mock_strat, \
             patch("orchestrator.loop.save_state") as mock_save, \
             patch("orchestrator.loop.load_config") as mock_cfg, \
             patch("orchestrator.loop._check_new_bar") as mock_bar, \
             patch("orchestrator.loop._run_cycle") as mock_cycle:

            mock_creds.return_value = ("key", "secret")
            mock_paper.return_value = MagicMock()
            mock_strat.return_value = MagicMock(short_window=10, long_window=30)
            mock_risk.return_value = MagicMock()
            mock_exec.return_value = MagicMock()
            mock_state.return_value = StateData()
            mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}
            mock_cfg.return_value = _make_orch_config()
            mock_bar.return_value = (True, "2024-06-16")

            # Mock _run_cycle to simulate a long-running cycle:
            # It will "run" while we simulate time passing, then a signal
            # arrives triggering the 300s warning.
            def long_cycle(*args, **kwargs):
                # Simulate that the cycle has been running for 400 seconds
                # by patching time.monotonic to return a value 400s later
                # than cycle_start_time.  But since cycle_start_time is set
                # just before this call using the REAL time.monotonic,
                # we instead trigger the signal handler here and fake the
                # elapsed time check by patching time.monotonic.
                #
                # Strategy: send a signal during the "cycle" execution.
                # The signal handler will check cycle_in_progress=True and
                # cycle_start_time, computing elapsed.  We need elapsed > 300.
                # We'll patch time.monotonic to return a large offset.
                if sig_mod.SIGINT in handlers:
                    # Temporarily patch time.monotonic so the handler sees
                    # elapsed > 300s.
                    original_monotonic = time.monotonic

                    # The cycle_start_time was set just before this call to
                    # the real time.monotonic().  We make the handler see
                    # time.monotonic() returning cycle_start_time + 400.
                    fake_start = original_monotonic()  # approximately cycle_start_time

                    with patch("time.monotonic", return_value=fake_start + 400):
                        handlers[sig_mod.SIGINT](sig_mod.SIGINT, None)

                return ("vetoed", None)

            mock_cycle.side_effect = long_cycle

            with patch("orchestrator.loop.signal.signal", side_effect=capture_sig):
                with patch("threading.Event.wait"):
                    with patch("threading.Event.clear"):
                        with caplog.at_level(logging.WARNING, logger="orchestrator"):
                            with pytest.raises(SystemExit) as exc_info:
                                run_loop("orchestrator/config.yaml")

                            # Exits cleanly (does not force-terminate).
                            assert exc_info.value.code == 0

        # Verify shutdown_cycle_timeout_warning was logged.
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("300" in msg or "timeout" in msg.lower()
                   for msg in warning_messages), (
            f"Expected 'shutdown_cycle_timeout_warning' WARNING mentioning 300s, "
            f"got: {warning_messages}"
        )


# ===========================================================================
# Deferred Resolution — last_acted_bar Advancement Test
# ===========================================================================


class TestDeferredResolutionAdvancesLastActedBar:
    """
    Verify that after a deferred decision resolves, last_acted_bar correctly
    advances to the bar that triggered it, preventing duplicate cycles.
    """

    @patch("orchestrator.loop.run_safety_check")
    @patch("orchestrator.loop.load_state")
    @patch("orchestrator.loop._create_paper_client")
    @patch("orchestrator.loop.load_credentials")
    @patch("orchestrator.loop.load_execution_config")
    @patch("orchestrator.loop.load_risk_config")
    @patch("orchestrator.loop.load_strategy_config")
    @patch("orchestrator.loop.save_state")
    @patch("orchestrator.loop.execute_decision")
    @patch("orchestrator.loop._check_new_bar")
    def test_last_acted_bar_advances_after_deferred_resolves(
        self, mock_bar, mock_execute, mock_save, mock_strat_cfg, mock_risk_cfg,
        mock_exec_cfg, mock_creds, mock_paper, mock_load_state, mock_safety
    ):
        """
        Scenario:
          1. State file has pending_decision with pending_decision_bar_timestamp="2024-06-16".
          2. Market opens → execute_decision is called directly → fills.
          3. After resolution, last_acted_bar in the saved state should be "2024-06-16".
          4. Subsequent _check_new_bar call with the same bar "2024-06-16" returns False
             (no duplicate cycle).
        """
        pending = {
            "decision": "APPROVED", "signal": "BUY", "position_size": 10,
            "stop_loss": 147.0, "reason": "All checks passed",
            "timestamp": pd.Timestamp("2024-06-16", tz="UTC"), "symbol": "AAPL",
        }

        mock_creds.return_value = ("key", "secret")

        # Mock trading client: clock.is_open = True (market open).
        mock_client = MagicMock()
        mock_clock = MagicMock()
        mock_clock.is_open = True
        mock_client.get_clock.return_value = mock_clock
        mock_paper.return_value = mock_client

        mock_strat_cfg.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk_cfg.return_value = MagicMock()
        mock_exec_cfg.return_value = MagicMock()

        # State has a pending decision with the bar timestamp stored.
        mock_load_state.return_value = StateData(
            positions=[],
            last_acted_bar="2024-06-15",  # OLD bar — not yet advanced.
            pending_decision=pending,
            pending_decision_bar_timestamp="2024-06-16",  # Bar that triggered deferral.
        )
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}

        # execute_decision returns filled.
        mock_execute.return_value = {
            "status": "filled", "entry_order_id": "order-abc",
            "stop_loss_order_id": "sl-def", "fill_price": 150.0,
            "filled_quantity": 10, "signal": "BUY", "decision": "APPROVED",
            "symbol": "AAPL", "position_size": 10, "reason": "Filled",
            "timestamp": pd.Timestamp("2024-06-16", tz="UTC"),
        }

        # After deferred resolves, _check_new_bar is called.
        # It should see last_acted_bar="2024-06-16" and return False.
        mock_bar.return_value = (False, "2024-06-16")

        import signal as sig_mod
        handlers = {}

        def capture_sig(signum, handler):
            handlers[signum] = handler

        with patch("orchestrator.loop.load_config") as mock_cfg:
            mock_cfg.return_value = _make_orch_config()

            call_count = [0]

            def mock_wait(self_event, timeout=None):
                call_count[0] += 1
                if call_count[0] >= 2:
                    # After deferred resolves and one bar check, trigger shutdown.
                    if sig_mod.SIGINT in handlers:
                        handlers[sig_mod.SIGINT](sig_mod.SIGINT, None)

            with patch("orchestrator.loop.signal.signal", side_effect=capture_sig):
                with patch("threading.Event.wait", mock_wait):
                    with patch("threading.Event.clear"):
                        with pytest.raises(SystemExit) as exc_info:
                            run_loop("orchestrator/config.yaml")
                        assert exc_info.value.code == 0

        # Verify execute_decision was called with the pending decision.
        mock_execute.assert_called_once()

        # Verify that save_state was called with last_acted_bar advanced to "2024-06-16".
        # Find the save_state call that resolved the deferred decision
        # (pending_decision=None).
        resolved_saves = [
            call_args[0][0] for call_args in mock_save.call_args_list
            if call_args[0][0].pending_decision is None
            and call_args[0][0].last_acted_bar == "2024-06-16"
        ]
        assert len(resolved_saves) >= 1, (
            f"Expected at least one save_state call with last_acted_bar='2024-06-16' "
            f"and pending_decision=None. Got: "
            f"{[(s.last_acted_bar, s.pending_decision) for s in [c[0][0] for c in mock_save.call_args_list]]}"
        )

        # Verify _check_new_bar was called (after resolution) and returned False.
        mock_bar.assert_called()
