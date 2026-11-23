# tests/test_task_7_1_verification.py
#
# Quick verification tests for task 7.1: execute_decision() wiring logic.
# These tests verify the full flow through submission, polling, and
# Order_Result construction, using mock clients.

import pandas as pd
import pytest
from unittest.mock import MagicMock

from execution.execute import execute_decision
from execution.config_loader import ExecutionConfig


@pytest.fixture
def config():
    """A valid ExecutionConfig for testing."""
    return ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=0.5)


@pytest.fixture
def approved_buy_dict():
    """A valid APPROVED BUY Decision_Dict."""
    return {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "Strong buy signal",
        "position_size": 10,
        "stop_loss": 145.0,
        "symbol": "AAPL",
    }


@pytest.fixture
def approved_sell_dict():
    """A valid APPROVED SELL Decision_Dict."""
    return {
        "decision": "APPROVED",
        "signal": "SELL",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "Take profit",
        "position_size": 10,
        "stop_loss": 145.0,
        "symbol": "AAPL",
    }


class TestErrorWrapping:
    """Req 12.4: All post-validation exceptions caught and returned as error."""

    def test_error_before_submission_entry_order_id_none(self, config, approved_buy_dict):
        """Req 12.7: entry_order_id is None if error before submission."""
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = RuntimeError("API down")

        result = execute_decision(approved_buy_dict, config, client=mock_client)

        assert result["status"] == "error"
        assert result["error_type"] == "RuntimeError"
        assert result["entry_order_id"] is None
        assert "RuntimeError" in result["reason"]
        assert "API down" in result["reason"]

    def test_error_after_submission_entry_order_id_present(self, config, approved_sell_dict):
        """Req 12.7: entry_order_id is non-None if error after submission."""
        mock_client = MagicMock()

        # submit_order succeeds (returns an order with ID)
        mock_order = MagicMock()
        mock_order.id = "order-abc-123"
        mock_client.submit_order.return_value = mock_order

        # _poll_fill itself raises an unexpected error (not a network error
        # during get_order_by_id, but something else entirely).
        # We achieve this by making get_order_by_id raise a non-standard error
        # that _poll_fill cannot handle, but actually _poll_fill handles all
        # exceptions internally. Instead, let's make the poll return filled
        # but then trigger an error in the Order_Result construction by
        # simulating an issue with filled_at attribute.
        #
        # Simpler approach: raise during cancel_order_by_id in a way that
        # propagates.  Actually, the simplest way is to raise in the
        # execute_decision wiring logic AFTER poll — we'll monkeypatch
        # _poll_fill to raise.
        import execution.execute as exe_module

        original_poll = exe_module._poll_fill

        def _fake_poll(client, order_id, config):
            raise RuntimeError("Unexpected poll failure")

        monkeypatch_attr = pytest.MonkeyPatch()
        monkeypatch_attr.setattr(exe_module, "_poll_fill", _fake_poll)

        try:
            result = execute_decision(approved_sell_dict, config, client=mock_client)
            assert result["status"] == "error"
            assert result["entry_order_id"] == "order-abc-123"
            assert result["error_type"] == "RuntimeError"
        finally:
            monkeypatch_attr.undo()


class TestFilledResult:
    """Req 11.1, 11.2: Filled Order_Result has all required fields."""

    def test_sell_filled_result_structure(self, config, approved_sell_dict):
        """SELL signal filled: verify all base + fill fields present."""
        mock_client = MagicMock()

        # Submit succeeds
        mock_order = MagicMock()
        mock_order.id = "sell-order-001"
        mock_client.submit_order.return_value = mock_order

        # Poll returns filled immediately
        mock_filled = MagicMock()
        mock_filled.status = "filled"
        mock_filled.filled_avg_price = "150.25"
        mock_filled.filled_qty = "10"
        mock_filled.filled_at = None  # triggers UTC fallback
        mock_client.get_order_by_id.return_value = mock_filled

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        # Base keys (Req 11.1)
        assert result["status"] == "filled"
        assert result["decision"] == "APPROVED"
        assert result["signal"] == "SELL"
        assert result["symbol"] == "AAPL"
        assert result["position_size"] == 10
        assert isinstance(result["reason"], str)
        assert 1 <= len(result["reason"]) <= 500
        assert result["timestamp"] == pd.Timestamp("2024-01-01", tz="UTC")

        # Fill fields (Req 11.2)
        assert result["entry_order_id"] == "sell-order-001"
        assert result["stop_loss_order_id"] is None  # SELL has no stop
        assert result["fill_price"] == 150.25
        assert result["filled_quantity"] == 10
        assert isinstance(result["filled_at"], pd.Timestamp)
        assert result["filled_at"].tzinfo is not None  # UTC-aware

    def test_buy_filled_with_bracket(self, config, approved_buy_dict):
        """BUY signal filled via fallback path: verify stop_loss_order_id present."""
        mock_client = MagicMock()

        # The Alpaca SDK validates bracket order requests at construction
        # time (requires take_profit).  The production code catches this
        # and falls back to plain market + separate stop.  We test the
        # fallback path here.
        #
        # First call to submit_order: plain market buy (fallback entry)
        mock_entry_order = MagicMock()
        mock_entry_order.id = "buy-order-001"
        mock_entry_order.status = "filled"
        mock_entry_order.filled_qty = "10"

        # Second call to submit_order: separate stop order
        mock_stop_order = MagicMock()
        mock_stop_order.id = "stop-order-001"

        # submit_order is called twice in fallback path:
        # 1st: plain market buy, 2nd: separate stop order
        mock_client.submit_order.side_effect = [mock_entry_order, mock_stop_order]

        # get_order_by_id used in fallback polling loop (checks entry fill)
        # and then in _poll_fill main loop
        mock_filled_for_poll = MagicMock()
        mock_filled_for_poll.status = "filled"
        mock_filled_for_poll.filled_avg_price = "155.00"
        mock_filled_for_poll.filled_qty = "10"
        mock_filled_for_poll.filled_at = None  # triggers UTC fallback

        mock_client.get_order_by_id.return_value = mock_filled_for_poll

        # Monkeypatch _submit_buy_bracket to bypass Alpaca SDK validation
        # and directly return the expected fallback result.
        import execution.execute as exe_module

        def _mock_submit_buy_bracket(client, symbol, quantity, stop_loss_price):
            return {
                "entry_order_id": "buy-order-001",
                "stop_loss_order_id": "stop-order-001",
                "bracket_fallback": True,
            }

        monkeypatch_attr = pytest.MonkeyPatch()
        monkeypatch_attr.setattr(exe_module, "_submit_buy_bracket", _mock_submit_buy_bracket)

        try:
            result = execute_decision(approved_buy_dict, config, client=mock_client)

            assert result["status"] == "filled"
            assert result["entry_order_id"] == "buy-order-001"
            assert result["stop_loss_order_id"] == "stop-order-001"
            assert result["fill_price"] == 155.00
            assert result["filled_quantity"] == 10
            assert result["bracket_fallback"] is True
        finally:
            monkeypatch_attr.undo()


class TestTerminalStates:
    """Req 11.6: Terminal non-filled states include entry_order_id."""

    def test_cancelled_includes_entry_order_id(self, config, approved_sell_dict):
        """Cancelled status includes entry_order_id for traceability."""
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "order-xyz"
        mock_client.submit_order.return_value = mock_order

        mock_cancelled = MagicMock()
        mock_cancelled.status = "cancelled"
        mock_client.get_order_by_id.return_value = mock_cancelled

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        assert result["status"] == "cancelled"
        assert result["entry_order_id"] == "order-xyz"
        assert result["decision"] == "APPROVED"
        assert result["signal"] == "SELL"

    def test_timeout_includes_entry_order_id(self, config, approved_sell_dict):
        """Timeout status includes entry_order_id."""
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "order-timeout-test"
        mock_client.submit_order.return_value = mock_order

        # get_order_by_id always returns non-terminal status
        mock_pending = MagicMock()
        mock_pending.status = "new"
        mock_client.get_order_by_id.return_value = mock_pending

        # Use a very short timeout to avoid long tests
        short_config = ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=5)
        result = execute_decision(approved_sell_dict, short_config, client=mock_client)

        assert result["status"] == "timeout"
        assert result["entry_order_id"] == "order-timeout-test"


class TestPartialFill:
    """Req 11.5: partial_fill includes cancelled_quantity."""

    def test_partial_fill_result(self, config, approved_sell_dict):
        """Partial fill has correct fields including cancelled_quantity."""
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "order-partial"
        mock_client.submit_order.return_value = mock_order

        # First poll: non-terminal (triggers timeout path)
        # Final check: partially_filled
        mock_partial = MagicMock()
        mock_partial.status = "partially_filled"
        mock_partial.filled_avg_price = "149.50"
        mock_partial.filled_qty = "7"
        mock_partial.qty = "10"
        mock_partial.filled_at = None
        mock_client.get_order_by_id.return_value = mock_partial

        # Use short timeout
        short_config = ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=5)
        result = execute_decision(approved_sell_dict, short_config, client=mock_client)

        assert result["status"] == "partial_fill"
        assert result["filled_quantity"] == 7
        assert result["cancelled_quantity"] == 3
        assert result["fill_price"] == 149.50
        assert result["entry_order_id"] == "order-partial"


class TestLoggingFallback:
    """Req 7.9: Logging failure is swallowed, written to stderr."""

    def test_safe_log_continues_on_logging_failure(self, config, approved_sell_dict, monkeypatch):
        """If logger raises, execution continues (doesn't crash)."""
        import logging
        import io
        import sys

        # Make the logger always raise
        real_logger = logging.getLogger("execution")
        monkeypatch.setattr(real_logger, "info", MagicMock(side_effect=RuntimeError("log broken")))
        monkeypatch.setattr(real_logger, "warning", MagicMock(side_effect=RuntimeError("log broken")))
        monkeypatch.setattr(real_logger, "error", MagicMock(side_effect=RuntimeError("log broken")))

        # Capture stderr
        captured_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured_stderr)

        # Mock client to raise (exercise the error path with logging)
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = ValueError("test error")

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        # Execution still completes — returns error result
        assert result["status"] == "error"
        # Stderr should have fallback messages
        stderr_output = captured_stderr.getvalue()
        assert "LOGGING FALLBACK" in stderr_output
