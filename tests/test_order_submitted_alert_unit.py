"""
tests/test_order_submitted_alert_unit.py — Unit tests for the order_submitted
and order_filled alert conditions in the orchestrator loop.

Verifies that:
  - order_submitted fires for any outcome with a non-None entry_order_id
    (filled, partial_fill, cancelled, rejected, expired, timeout)
  - order_submitted does NOT fire when entry_order_id is None
    (skipped, vetoed, no_action, error-without-submission)
  - order_filled only fires for filled/partial_fill outcomes

These tests exercise the alert logic in the Step 5b section of run_loop()
by calling run_loop with mocked components and triggering shutdown after
one cycle.

Validates: Requirements 6.3, 6.4

Run with:  python -m pytest tests/test_order_submitted_alert_unit.py -v
"""

import logging
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.config_loader import OrchestratorConfig
from orchestrator.state_manager import StateData
from orchestrator.loop import run_loop


# ===========================================================================
# Fixtures
# ===========================================================================


def _make_orch_config():
    """Standard OrchestratorConfig for testing."""
    return OrchestratorConfig(
        bar_check_interval_seconds=300.0,
        symbol="AAPL",
        state_file_path="orchestrator/state.json",
        kill_switch_file_path="orchestrator/KILL_SWITCH",
    )


def _run_one_cycle_and_capture_alerts(outcome, order_result):
    """
    Run one iteration of run_loop with a mocked _run_cycle that returns
    the given (outcome, order_result) pair, then the loop is terminated
    via stop_flag on the second iteration.

    Returns the list of (event_type, context) tuples passed to alerting.send_alert.
    """
    alert_calls = []

    def capture_alert(event_type, context):
        alert_calls.append((event_type, context))

    # We use the same pattern as TestShutdownSignalSetsFlag:
    # patch threading.Event.wait to trigger the signal handler on first call.
    import signal as sig_module

    call_count = [0]
    captured_handler = [None]

    def capture_signal(signum, handler):
        if signum == sig_module.SIGINT:
            captured_handler[0] = handler

    def mock_event_wait(timeout=None):
        """
        On first call: trigger SIGINT handler (sets stop_flag).
        The next loop iteration will see stop_flag=True and exit cleanly.
        """
        call_count[0] += 1
        if call_count[0] == 1 and captured_handler[0]:
            captured_handler[0](sig_module.SIGINT, None)

    with patch("orchestrator.loop.load_config") as mock_cfg, \
         patch("orchestrator.loop.load_credentials") as mock_creds, \
         patch("orchestrator.loop._create_paper_client") as mock_paper, \
         patch("orchestrator.loop.load_strategy_config") as mock_strat, \
         patch("orchestrator.loop.load_risk_config") as mock_risk, \
         patch("orchestrator.loop.load_execution_config") as mock_exec, \
         patch("orchestrator.loop.load_state") as mock_state, \
         patch("orchestrator.loop.run_safety_check") as mock_safety, \
         patch("orchestrator.loop.save_state"), \
         patch("orchestrator.loop._check_new_bar") as mock_bar, \
         patch("orchestrator.loop._run_cycle") as mock_cycle, \
         patch("orchestrator.loop._check_kill_switch", return_value=False), \
         patch("orchestrator.loop.alerting.initialize"), \
         patch("orchestrator.loop.alerting.send_alert", side_effect=capture_alert), \
         patch("orchestrator.loop.signal.signal", side_effect=capture_signal), \
         patch("threading.Event.wait", side_effect=mock_event_wait), \
         patch("threading.Event.clear"):

        mock_cfg.return_value = _make_orch_config()
        mock_creds.return_value = ("key", "secret")
        mock_paper.return_value = MagicMock()
        mock_strat.return_value = MagicMock(short_window=10, long_window=30)
        mock_risk.return_value = MagicMock()
        mock_exec.return_value = MagicMock()
        mock_state.return_value = StateData()
        mock_safety.return_value = {"total_positions": 0, "unprotected_count": 0}
        mock_bar.return_value = (True, "2024-06-16")
        mock_cycle.return_value = (outcome, order_result)

        with pytest.raises(SystemExit) as exc_info:
            run_loop("orchestrator/config.yaml")

        # Should exit 0 (graceful shutdown).
        assert exc_info.value.code == 0

    return alert_calls


# ===========================================================================
# Test: order_submitted fires for outcomes with entry_order_id present
# ===========================================================================


class TestOrderSubmittedFiresWhenOrderPlaced:
    """
    Requirement 6.4: order_submitted fires for ANY outcome where an order
    was actually placed (entry_order_id is not None).
    """

    @pytest.mark.parametrize("outcome", [
        "cancelled", "rejected", "expired", "timeout",
    ])
    def test_order_submitted_fires_for_non_fill_outcomes(self, outcome):
        """
        When the outcome is cancelled/rejected/expired/timeout but an order
        was submitted (entry_order_id is set), order_submitted alert fires.
        """
        order_result = {
            "status": outcome,
            "entry_order_id": "ord-abc-123",
            "symbol": "AAPL",
        }

        alert_calls = _run_one_cycle_and_capture_alerts(outcome, order_result)
        event_types = [c[0] for c in alert_calls]

        assert "order_submitted" in event_types, (
            f"Expected order_submitted for outcome='{outcome}' with "
            f"entry_order_id set, got events: {event_types}"
        )

    @pytest.mark.parametrize("outcome", ["filled", "partial_fill"])
    def test_order_submitted_fires_for_fill_outcomes(self, outcome):
        """
        When the outcome is filled/partial_fill, order_submitted fires
        (in addition to order_filled).
        """
        order_result = {
            "status": outcome,
            "entry_order_id": "ord-xyz-789",
            "symbol": "AAPL",
            "filled_quantity": 10,
            "fill_price": 150.25,
            "signal": "BUY",
        }

        alert_calls = _run_one_cycle_and_capture_alerts(outcome, order_result)
        event_types = [c[0] for c in alert_calls]

        assert "order_submitted" in event_types
        assert "order_filled" in event_types


# ===========================================================================
# Test: order_submitted does NOT fire when no order was placed
# ===========================================================================


class TestOrderSubmittedDoesNotFireWithoutOrder:
    """
    order_submitted must NOT fire when entry_order_id is None (no order was
    submitted to Alpaca) or when order_result is None (cycle ended before
    reaching execution).
    """

    def test_no_alert_for_vetoed(self):
        """
        Vetoed cycles have order_result=None — no order_submitted alert.
        """
        alert_calls = _run_one_cycle_and_capture_alerts("vetoed", None)
        event_types = [c[0] for c in alert_calls]

        assert "order_submitted" not in event_types
        assert "order_filled" not in event_types

    def test_no_alert_for_no_action(self):
        """
        NO_ACTION cycles have order_result=None — no order_submitted alert.
        """
        alert_calls = _run_one_cycle_and_capture_alerts("no_action", None)
        event_types = [c[0] for c in alert_calls]

        assert "order_submitted" not in event_types

    def test_no_alert_for_skipped_no_order_id(self):
        """
        Skipped outcomes with entry_order_id=None — no order_submitted.
        """
        order_result = {
            "status": "skipped",
            "entry_order_id": None,
        }
        alert_calls = _run_one_cycle_and_capture_alerts("skipped", order_result)
        event_types = [c[0] for c in alert_calls]

        assert "order_submitted" not in event_types


# ===========================================================================
# Test: order_filled ONLY fires for filled/partial_fill
# ===========================================================================


class TestOrderFilledOnlyForFills:
    """order_filled must only fire for filled/partial_fill outcomes."""

    @pytest.mark.parametrize("outcome", [
        "cancelled", "rejected", "expired", "timeout",
    ])
    def test_order_filled_does_not_fire_for_non_fill_outcomes(self, outcome):
        """
        Even when entry_order_id is set, order_filled does NOT fire for
        non-fill terminal outcomes.
        """
        order_result = {
            "status": outcome,
            "entry_order_id": "ord-123",
            "symbol": "AAPL",
        }

        alert_calls = _run_one_cycle_and_capture_alerts(outcome, order_result)
        event_types = [c[0] for c in alert_calls]

        assert "order_filled" not in event_types, (
            f"order_filled should not fire for outcome='{outcome}'"
        )
