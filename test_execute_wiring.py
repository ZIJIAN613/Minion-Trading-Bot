"""
Validation tests for Task 7.1 — execute_decision() wiring logic.

Tests the full flow: submission → polling → Order_Result construction
using mock clients to avoid real network I/O.
"""
import sys
import pandas as pd
from unittest.mock import MagicMock, patch, PropertyMock
from execution import execute_decision
from execution.config_loader import ExecutionConfig

config = ExecutionConfig(fill_timeout_seconds=5.0, poll_interval_seconds=0.5)


# ---------------------------------------------------------------------------
# Helper: create a mock client with configurable behavior
# ---------------------------------------------------------------------------
def make_mock_client(
    submit_raises=None,
    entry_order_id="entry-123",
    stop_leg_id="stop-456",
    has_legs=True,
    poll_status="filled",
    fill_price=155.50,
    filled_qty=10,
    filled_at="2024-01-01T10:30:00+00:00",
    total_qty=10,
):
    """Create a mock TradingClient with predictable behavior."""
    client = MagicMock()

    if submit_raises:
        client.submit_order.side_effect = submit_raises
        return client

    # Mock submit_order response
    mock_order = MagicMock()
    mock_order.id = entry_order_id

    if has_legs:
        mock_leg = MagicMock()
        mock_leg.order_type = "stop"
        mock_leg.id = stop_leg_id
        mock_order.legs = [mock_leg]
    else:
        mock_order.legs = []

    client.submit_order.return_value = mock_order

    # Mock get_order_by_id for polling
    mock_polled = MagicMock()
    mock_polled.status = poll_status
    mock_polled.filled_avg_price = str(fill_price)
    mock_polled.filled_qty = str(filled_qty)
    mock_polled.filled_at = filled_at
    mock_polled.qty = str(total_qty)
    client.get_order_by_id.return_value = mock_polled

    return client


# ---------------------------------------------------------------------------
# Test 1: BUY signal — filled scenario
# ---------------------------------------------------------------------------
def test_buy_filled():
    client = make_mock_client(poll_status="filled", fill_price=155.50, filled_qty=10)
    decision = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "All risk checks passed",
        "position_size": 10,
        "stop_loss": 145.0,
        "symbol": "AAPL",
    }
    result = execute_decision(decision, config, client=client)

    assert result["status"] == "filled", f"Expected filled, got {result['status']}"
    assert result["decision"] == "APPROVED"
    assert result["signal"] == "BUY"
    assert result["symbol"] == "AAPL"
    assert result["position_size"] == 10
    assert result["entry_order_id"] is not None
    assert result["fill_price"] == 155.50
    assert result["filled_quantity"] == 10
    assert "reason" in result and len(result["reason"]) > 0
    assert "timestamp" in result
    assert "filled_at" in result
    print("Test 1 PASSED: BUY filled scenario")


# ---------------------------------------------------------------------------
# Test 2: SELL signal — filled scenario
# ---------------------------------------------------------------------------
def test_sell_filled():
    client = make_mock_client(
        poll_status="filled", fill_price=200.0, filled_qty=5, has_legs=False
    )
    decision = {
        "decision": "APPROVED",
        "signal": "SELL",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "Trend reversal detected",
        "position_size": 5,
        "stop_loss": 210.0,
        "symbol": "MSFT",
    }
    result = execute_decision(decision, config, client=client)

    assert result["status"] == "filled"
    assert result["signal"] == "SELL"
    assert result["stop_loss_order_id"] is None  # No stop for SELL
    assert result["fill_price"] == 200.0
    assert result["filled_quantity"] == 5
    print("Test 2 PASSED: SELL filled scenario")


# ---------------------------------------------------------------------------
# Test 3: Error before submission (client creation fails)
# ---------------------------------------------------------------------------
def test_error_before_submission():
    client = make_mock_client(submit_raises=RuntimeError("Connection refused"))
    decision = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "All checks passed",
        "position_size": 10,
        "stop_loss": 145.0,
        "symbol": "AAPL",
    }
    result = execute_decision(decision, config, client=client)

    assert result["status"] == "error"
    assert result["error_type"] == "RuntimeError"
    assert result["entry_order_id"] is None  # Failed before order ID assigned
    assert "Connection refused" in result["reason"]
    assert result["position_size"] == 10
    print("Test 3 PASSED: Error before submission returns correct error result")


# ---------------------------------------------------------------------------
# Test 4: Error after submission (polling fails)
# ---------------------------------------------------------------------------
def test_error_after_submission():
    client = MagicMock()
    # submit_order succeeds
    mock_order = MagicMock()
    mock_order.id = "submitted-789"
    mock_order.legs = []
    client.submit_order.return_value = mock_order
    # get_order_by_id (polling) raises
    client.get_order_by_id.side_effect = RuntimeError("Network timeout")

    decision = {
        "decision": "APPROVED",
        "signal": "SELL",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "Trend reversal",
        "position_size": 3,
        "stop_loss": 180.0,
        "symbol": "GOOG",
    }
    result = execute_decision(decision, config, client=client)

    # _poll_fill will fail with re-query (both attempts fail), then final
    # check also fails, returning "timeout" from _poll_fill.  Then the
    # wiring logic returns a timeout result.
    # Actually, since get_order_by_id always raises, _poll_fill's
    # _query_order_with_retry will raise on both attempts, breaking the loop.
    # Then the final check also raises. _poll_fill returns {"status": "timeout"}.
    # So the wiring layer returns a timeout result, NOT an error.
    assert result["status"] == "timeout"
    assert result["entry_order_id"] == "submitted-789"
    print("Test 4 PASSED: Polling failure after submission returns timeout with order ID")


# ---------------------------------------------------------------------------
# Test 5: Order_Result base keys always present
# ---------------------------------------------------------------------------
def test_base_keys_always_present():
    """Every result must have the 7 base keys (Requirement 11.1)."""
    base_keys = {"status", "decision", "signal", "symbol", "position_size", "reason", "timestamp"}

    # Test with error result
    client = make_mock_client(submit_raises=ValueError("Bad request"))
    decision = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "Passed",
        "position_size": 1,
        "stop_loss": 50.0,
        "symbol": "XYZ",
    }
    result = execute_decision(decision, config, client=client)
    missing = base_keys - set(result.keys())
    assert not missing, f"Missing base keys: {missing}"

    # Test with filled result
    client2 = make_mock_client(poll_status="filled")
    result2 = execute_decision(decision, config, client=client2)
    missing2 = base_keys - set(result2.keys())
    assert not missing2, f"Missing base keys in filled: {missing2}"

    print("Test 5 PASSED: Base keys always present in Order_Result")


# ---------------------------------------------------------------------------
# Test 6: reason is 1-500 chars (Requirement 11.4)
# ---------------------------------------------------------------------------
def test_reason_length():
    # Error with very long exception message
    long_msg = "x" * 1000
    client = make_mock_client(submit_raises=RuntimeError(long_msg))
    decision = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "Passed",
        "position_size": 1,
        "stop_loss": 50.0,
        "symbol": "A",
    }
    result = execute_decision(decision, config, client=client)

    assert len(result["reason"]) <= 500, f"Reason too long: {len(result['reason'])}"
    assert len(result["reason"]) >= 1
    print("Test 6 PASSED: Reason truncated to 500 chars")


# ---------------------------------------------------------------------------
# Test 7: Partial fill scenario
# ---------------------------------------------------------------------------
def test_partial_fill():
    client = make_mock_client(
        poll_status="partially_filled",
        fill_price=100.0,
        filled_qty=7,
        total_qty=10,
    )
    decision = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "All checks passed",
        "position_size": 10,
        "stop_loss": 90.0,
        "symbol": "TSLA",
    }
    result = execute_decision(decision, config, client=client)

    assert result["status"] == "partial_fill"
    assert result["filled_quantity"] == 7
    assert result["cancelled_quantity"] == 3
    assert result["fill_price"] == 100.0
    assert result["position_size"] == 10  # Original requested qty
    print("Test 7 PASSED: Partial fill scenario")


# ---------------------------------------------------------------------------
# Test 8: Cancelled/expired/rejected terminal states
# ---------------------------------------------------------------------------
def test_terminal_states():
    for status in ("cancelled", "expired", "rejected"):
        client = make_mock_client(poll_status=status)
        decision = {
            "decision": "APPROVED",
            "signal": "BUY",
            "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
            "reason": "Passed",
            "position_size": 5,
            "stop_loss": 140.0,
            "symbol": "AAPL",
        }
        result = execute_decision(decision, config, client=client)
        assert result["status"] == status, f"Expected {status}, got {result['status']}"
        assert result["entry_order_id"] is not None
        assert f"Order {status}" in result["reason"]

    print("Test 8 PASSED: Terminal states (cancelled/expired/rejected)")


# ---------------------------------------------------------------------------
# Test 9: Timeout scenario
# ---------------------------------------------------------------------------
def test_timeout():
    client = make_mock_client(poll_status="new")  # Non-terminal — will timeout
    # Make the polling always return "new" (non-terminal)
    mock_order = MagicMock()
    mock_order.status = "new"
    mock_order.filled_avg_price = "0"
    mock_order.filled_qty = "0"
    mock_order.filled_at = None
    mock_order.qty = "10"
    client.get_order_by_id.return_value = mock_order

    decision = {
        "decision": "APPROVED",
        "signal": "SELL",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "Sell signal",
        "position_size": 10,
        "stop_loss": 200.0,
        "symbol": "NVDA",
    }
    # Use a very short timeout for this test
    short_config = ExecutionConfig(fill_timeout_seconds=5.0, poll_interval_seconds=0.5)
    result = execute_decision(decision, short_config, client=client)

    assert result["status"] == "timeout"
    assert result["entry_order_id"] is not None
    assert "timed out" in result["reason"]
    print("Test 9 PASSED: Timeout scenario")


# ---------------------------------------------------------------------------
# Test 10: Logging failure is swallowed (Requirement 7.9)
# ---------------------------------------------------------------------------
def test_logging_failure_swallowed():
    """If logger raises, execution continues via stderr fallback."""
    client = make_mock_client(poll_status="filled")
    decision = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "reason": "All checks passed",
        "position_size": 5,
        "stop_loss": 140.0,
        "symbol": "AAPL",
    }
    # Patch the logger to raise — _safe_log should catch it
    with patch("execution.execute.logger") as mock_logger:
        mock_logger.info.side_effect = RuntimeError("Logger broken")
        mock_logger.warning.side_effect = RuntimeError("Logger broken")
        mock_logger.error.side_effect = RuntimeError("Logger broken")

        result = execute_decision(decision, config, client=client)
        # Should still return a result despite logging failures
        assert result["status"] == "filled"
        print("Test 10 PASSED: Logging failure swallowed, execution continues")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_buy_filled()
    test_sell_filled()
    test_error_before_submission()
    test_error_after_submission()
    test_base_keys_always_present()
    test_reason_length()
    test_partial_fill()
    test_terminal_states()
    test_timeout()
    test_logging_failure_swallowed()
    print("\nALL WIRING TESTS PASSED")
