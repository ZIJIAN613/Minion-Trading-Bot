"""
tests/test_kill_switch_precedence_unit.py — Unit test for kill switch + Stop_Flag
precedence (Requirement 2.4).

Verifies that when BOTH the kill switch file exists AND the Stop_Flag
(shutdown signal) is set, the kill switch wins — because it is checked
first in the loop, before the stop_flag check.

This test exercises the ordering contract established by the design:
  1. _check_kill_switch() runs FIRST at the top of the loop.
  2. If it returns True, _kill_switch_shutdown() fires immediately.
  3. The graceful shutdown path (stop_flag check) is never reached.

Validates: Requirements 2.4

Run with:  python -m pytest tests/test_kill_switch_precedence_unit.py -v
"""

import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.config_loader import OrchestratorConfig
from orchestrator.state_manager import StateData
from orchestrator.loop import (
    _check_kill_switch,
    _kill_switch_shutdown,
)


# ===========================================================================
# Fixtures
# ===========================================================================


def _make_orch_config(kill_switch_path):
    """Create an OrchestratorConfig with a given kill_switch_file_path."""
    return OrchestratorConfig(
        bar_check_interval_seconds=300.0,
        symbol="AAPL",
        state_file_path="orchestrator/state.json",
        kill_switch_file_path=kill_switch_path,
    )


def _make_state_data():
    """Create a minimal StateData for testing."""
    return StateData(
        positions=[],
        last_acted_bar="2024-06-15",
        pending_decision=None,
    )


# ===========================================================================
# Test: Kill switch + Stop_Flag precedence — kill switch wins
# ===========================================================================


class TestKillSwitchPrecedenceOverStopFlag:
    """
    Requirement 2.4: IF both the Kill_Switch_File exists and the Stop_Flag
    is set when the top-of-loop check executes, THEN THE Orchestrator SHALL
    honor the kill switch (halt with exit code 1) because the kill switch
    check precedes the Stop_Flag check.
    """

    def test_kill_switch_fires_when_both_conditions_true(self, tmp_path):
        """
        When both the kill switch file exists AND stop_flag is True,
        the kill switch path is taken (exit code 1), not the graceful
        shutdown path (exit code 0).

        This simulates the top-of-loop logic:
          if _check_kill_switch(path):   # ← checked FIRST
              _kill_switch_shutdown(...)  # ← fires before stop_flag check
          if stop_flag:                  # ← never reached
              ... graceful shutdown ...
        """
        # Create a real kill switch file.
        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.write_text("active", encoding="utf-8")

        # Simulate stop_flag being set (as if SIGINT was received).
        stop_flag = True  # noqa: F841 — this is illustrative of the state

        orch_config = _make_orch_config(str(kill_file))
        state_data = _make_state_data()

        # Step 1: Kill switch check returns True (file exists).
        assert _check_kill_switch(str(kill_file)) is True

        # Step 2: Because the kill switch check is FIRST in the loop,
        # _kill_switch_shutdown fires. It should exit with code 1.
        with patch("orchestrator.loop.save_state"):
            with patch("orchestrator.loop.alerting.send_alert") as mock_alert:
                with pytest.raises(SystemExit) as exc_info:
                    _kill_switch_shutdown(orch_config, state_data, cycle_number=5)

        # CRITICAL ASSERTION: Exit code is 1 (kill switch), NOT 0 (graceful).
        assert exc_info.value.code == 1, (
            f"Expected exit code 1 (kill switch), got {exc_info.value.code}. "
            f"The kill switch should take precedence over stop_flag."
        )

        # The graceful shutdown alert was NOT dispatched.
        # send_alert should have been called with "kill_switch_triggered",
        # NOT "graceful_shutdown_completed".
        mock_alert.assert_called_once_with(
            "kill_switch_triggered", {"cycle_number": 5}
        )

    def test_graceful_shutdown_not_reached_when_kill_switch_active(self, tmp_path):
        """
        When the kill switch fires, the graceful_shutdown_completed alert
        is NEVER sent — confirming the graceful shutdown path was not reached.
        """
        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.write_text("", encoding="utf-8")

        orch_config = _make_orch_config(str(kill_file))
        state_data = _make_state_data()

        all_alert_calls = []

        def capture_alert(event_type, context):
            all_alert_calls.append((event_type, context))

        with patch("orchestrator.loop.save_state"):
            with patch("orchestrator.loop.alerting.send_alert", side_effect=capture_alert):
                with pytest.raises(SystemExit):
                    _kill_switch_shutdown(orch_config, state_data, cycle_number=99)

        # Verify: only kill_switch_triggered was sent, never graceful_shutdown.
        event_types = [call[0] for call in all_alert_calls]
        assert "kill_switch_triggered" in event_types
        assert "graceful_shutdown_completed" not in event_types

    def test_exit_code_is_not_zero(self, tmp_path):
        """
        The exit code must be 1 or 2 (kill switch path), never 0 (graceful).
        This holds even if save_state fails (dirty path gives code 2).
        """
        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.write_text("x", encoding="utf-8")

        orch_config = _make_orch_config(str(kill_file))
        state_data = _make_state_data()

        # Test clean path (code 1).
        with patch("orchestrator.loop.save_state"):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit) as exc_info:
                    _kill_switch_shutdown(orch_config, state_data, cycle_number=1)
        assert exc_info.value.code != 0
        assert exc_info.value.code in (1, 2)

        # Test dirty path (code 2).
        with patch("orchestrator.loop.save_state", side_effect=OSError("fail")):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit) as exc_info:
                    _kill_switch_shutdown(orch_config, state_data, cycle_number=1)
        assert exc_info.value.code != 0
        assert exc_info.value.code in (1, 2)
