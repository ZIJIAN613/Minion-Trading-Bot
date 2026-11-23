"""
tests/test_kill_switch_shutdown_unit.py — Unit tests for the shutdown sequence
helpers: _kill_switch_shutdown() and _post_order_halt_shutdown().

Tests verify:
  - Exit code 1: clean kill switch shutdown (save_state succeeds)
  - Exit code 2: dirty kill switch shutdown (save_state raises OSError)
  - Exit code 3: clean post-order halt (save_state succeeds)
  - Exit code 4: dirty post-order halt (save_state raises OSError)
  - Kill switch file is NOT deleted after trigger

Validates: Requirements 1.2, 2.5, 6.6

Run with:  python -m pytest tests/test_kill_switch_shutdown_unit.py -v
"""

import logging
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.config_loader import OrchestratorConfig
from orchestrator.state_manager import StateData
from orchestrator.loop import _kill_switch_shutdown, _post_order_halt_shutdown


# ===========================================================================
# Fixtures
# ===========================================================================


def _make_orch_config(kill_switch_path="orchestrator/KILL_SWITCH"):
    """Create a standard OrchestratorConfig for testing shutdown sequences."""
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
# Test: _kill_switch_shutdown exit code 1 (clean)
# ===========================================================================


class TestKillSwitchShutdownClean:
    """Exit code 1: all shutdown steps succeed."""

    def test_exit_code_1_on_success(self, caplog):
        """
        When save_state succeeds, _kill_switch_shutdown exits with code 1.
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()

        with patch("orchestrator.loop.save_state") as mock_save:
            with patch("orchestrator.loop.alerting.send_alert") as mock_alert:
                with pytest.raises(SystemExit) as exc_info:
                    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                        _kill_switch_shutdown(orch_config, state_data, cycle_number=5)

        # Verify exit code 1.
        assert exc_info.value.code == 1

        # Verify save_state was called with correct args.
        mock_save.assert_called_once_with(state_data, "orchestrator/state.json")

        # Verify send_alert was called.
        mock_alert.assert_called_once_with(
            "kill_switch_triggered", {"cycle_number": 5}
        )

    def test_critical_log_fires_on_success(self, caplog):
        """
        The CRITICAL log with event="kill_switch_triggered" fires before exit.
        """
        orch_config = _make_orch_config("/tmp/MY_KILL_SWITCH")
        state_data = _make_state_data()

        with patch("orchestrator.loop.save_state"):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit):
                    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                        _kill_switch_shutdown(orch_config, state_data, cycle_number=10)

        # Find the CRITICAL log record.
        critical_records = [
            r for r in caplog.records
            if r.levelno == logging.CRITICAL
        ]
        assert len(critical_records) >= 1

        record = critical_records[0]
        assert record.__dict__.get("event") == "kill_switch_triggered"
        assert record.__dict__.get("file_path") == "/tmp/MY_KILL_SWITCH"


# ===========================================================================
# Test: _kill_switch_shutdown exit code 2 (dirty)
# ===========================================================================


class TestKillSwitchShutdownDirty:
    """Exit code 2: save_state raises, dirty shutdown path."""

    def test_exit_code_2_on_save_state_failure(self, caplog):
        """
        When save_state raises OSError, _kill_switch_shutdown exits with code 2.
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()

        with patch(
            "orchestrator.loop.save_state",
            side_effect=OSError("disk full"),
        ):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit) as exc_info:
                    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                        _kill_switch_shutdown(orch_config, state_data, cycle_number=3)

        assert exc_info.value.code == 2

    def test_warning_log_on_dirty_path(self, caplog):
        """
        The WARNING log with event="kill_switch_shutdown_failed" fires on the
        dirty path, with correct error_type and error_message.
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()

        with patch(
            "orchestrator.loop.save_state",
            side_effect=OSError("permission denied on state file"),
        ):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit):
                    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                        _kill_switch_shutdown(orch_config, state_data, cycle_number=7)

        # Find the WARNING log about shutdown failure.
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and r.__dict__.get("event") == "kill_switch_shutdown_failed"
        ]
        assert len(warning_records) == 1

        record = warning_records[0]
        assert record.__dict__.get("error_type") == "OSError"
        assert "permission denied" in record.__dict__.get("error_message", "")

    def test_error_message_truncated_to_200_chars(self, caplog):
        """
        When the exception message is very long, error_message is truncated
        to 200 characters.
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()

        long_message = "x" * 500

        with patch(
            "orchestrator.loop.save_state",
            side_effect=OSError(long_message),
        ):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit):
                    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                        _kill_switch_shutdown(orch_config, state_data, cycle_number=1)

        warning_records = [
            r for r in caplog.records
            if r.__dict__.get("event") == "kill_switch_shutdown_failed"
        ]
        assert len(warning_records) == 1
        error_msg = warning_records[0].__dict__.get("error_message", "")
        assert len(error_msg) <= 200


# ===========================================================================
# Test: _post_order_halt_shutdown exit code 3 (clean)
# ===========================================================================


class TestPostOrderHaltClean:
    """Exit code 3: all shutdown steps succeed."""

    def test_exit_code_3_on_success(self):
        """
        When save_state succeeds, _post_order_halt_shutdown exits with code 3.
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()
        error_context = {
            "error_details": "Order stuck in pending state",
            "symbol": "AAPL",
            "entry_order_id": "ord-abc-123",
        }

        with patch("orchestrator.loop.save_state") as mock_save:
            with patch("orchestrator.loop.alerting.send_alert") as mock_alert:
                with pytest.raises(SystemExit) as exc_info:
                    _post_order_halt_shutdown(
                        orch_config, state_data, cycle_number=12,
                        error_context=error_context,
                    )

        assert exc_info.value.code == 3
        mock_save.assert_called_once_with(state_data, "orchestrator/state.json")

    def test_send_alert_called_with_correct_context(self):
        """
        send_alert is called with "halt_the_loop_error" and the correct
        context dict (error_summary, symbol, order_id).
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()
        error_context = {
            "error_details": "Timeout waiting for fill confirmation",
            "symbol": "SPY",
            "entry_order_id": "ord-xyz-789",
        }

        with patch("orchestrator.loop.save_state"):
            with patch("orchestrator.loop.alerting.send_alert") as mock_alert:
                with pytest.raises(SystemExit):
                    _post_order_halt_shutdown(
                        orch_config, state_data, cycle_number=4,
                        error_context=error_context,
                    )

        mock_alert.assert_called_once_with("halt_the_loop_error", {
            "error_summary": "Timeout waiting for fill confirmation"[:200],
            "symbol": "SPY",
            "order_id": "ord-xyz-789",
        })


# ===========================================================================
# Test: _post_order_halt_shutdown exit code 4 (dirty)
# ===========================================================================


class TestPostOrderHaltDirty:
    """Exit code 4: save_state raises, dirty halt path."""

    def test_exit_code_4_on_save_state_failure(self, caplog):
        """
        When save_state raises OSError, _post_order_halt_shutdown exits
        with code 4.
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()
        error_context = {"error_details": "test", "symbol": "X", "entry_order_id": "o1"}

        with patch(
            "orchestrator.loop.save_state",
            side_effect=OSError("no space left on device"),
        ):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit) as exc_info:
                    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                        _post_order_halt_shutdown(
                            orch_config, state_data, cycle_number=2,
                            error_context=error_context,
                        )

        assert exc_info.value.code == 4

    def test_warning_log_on_dirty_path(self, caplog):
        """
        The WARNING log with event="post_order_halt_shutdown_failed" fires
        with the correct error_type and error_message.
        """
        orch_config = _make_orch_config()
        state_data = _make_state_data()
        error_context = {"error_details": "e", "symbol": "Y", "entry_order_id": "o2"}

        with patch(
            "orchestrator.loop.save_state",
            side_effect=OSError("readonly filesystem"),
        ):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit):
                    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                        _post_order_halt_shutdown(
                            orch_config, state_data, cycle_number=9,
                            error_context=error_context,
                        )

        warning_records = [
            r for r in caplog.records
            if r.__dict__.get("event") == "post_order_halt_shutdown_failed"
        ]
        assert len(warning_records) == 1

        record = warning_records[0]
        assert record.__dict__.get("error_type") == "OSError"
        assert "readonly" in record.__dict__.get("error_message", "")


# ===========================================================================
# Test: Kill switch file NOT deleted after trigger
# ===========================================================================


class TestKillSwitchFileNotDeleted:
    """The kill switch file must remain on disk after triggering."""

    def test_file_persists_after_shutdown(self, tmp_path):
        """
        After _kill_switch_shutdown() fires, the kill switch file must still
        exist on disk — the developer must manually remove it to re-enable
        trading (Requirement 2.5).
        """
        # Create a real kill switch file in the temp directory.
        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.write_text("triggered", encoding="utf-8")

        orch_config = _make_orch_config(kill_switch_path=str(kill_file))
        state_data = _make_state_data()

        with patch("orchestrator.loop.save_state"):
            with patch("orchestrator.loop.alerting.send_alert"):
                with pytest.raises(SystemExit) as exc_info:
                    _kill_switch_shutdown(orch_config, state_data, cycle_number=1)

        # Verify the function exited (code 1 = clean).
        assert exc_info.value.code == 1

        # CRITICAL: The kill switch file must STILL exist on disk.
        assert kill_file.exists(), (
            "Kill switch file was deleted after trigger — "
            "it should persist until manually removed by the developer."
        )
