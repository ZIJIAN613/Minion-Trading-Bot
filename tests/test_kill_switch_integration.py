"""
tests/test_kill_switch_integration.py — Integration tests for kill switch and
alerting, exercising the real run_loop() wiring from task 6.6.

Four end-to-end scenarios:
  1. Kill switch file present at loop start → exit code 1, CRITICAL log, alert
  2. Kill switch detected during _interruptible_wait → returns "kill_switch"
  3. End-to-end alert delivery with mocked Telegram API
  4. Orchestrator continues when alerting.initialize() fails

These tests exercise the actual run_loop() integration (not helper functions
in isolation), catching wiring bugs that unit tests can't see.

Validates: Requirements 1.1, 1.2, 2.6, 3.1, 3.4, 6.1

Run with:  python -m pytest tests/test_kill_switch_integration.py -v
"""

import json
import logging
import os
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.config_loader import OrchestratorConfig
from orchestrator.state_manager import StateData
from orchestrator.loop import (
    run_loop,
    _check_kill_switch,
    _interruptible_wait,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_orch_config(kill_switch_path):
    """Create an OrchestratorConfig with a specific kill switch path."""
    return OrchestratorConfig(
        bar_check_interval_seconds=300.0,
        symbol="AAPL",
        state_file_path="orchestrator/state.json",
        kill_switch_file_path=kill_switch_path,
    )


# ===========================================================================
# Scenario 1: Full loop iteration with kill switch file present
# ===========================================================================


class TestKillSwitchAtLoopStart:
    """
    Integration test: run_loop() with a kill switch file already present
    on disk before the loop starts.  Exercises the real top-of-loop check
    wired in task 6.6 (not just the helper function in isolation).
    """

    def test_exit_code_1_and_critical_log_and_alert(self, tmp_path, caplog):
        """
        When the kill switch file exists before run_loop() enters its main
        loop, the process exits with code 1, the CRITICAL log fires, and
        send_alert("kill_switch_triggered") is called.
        """
        # Create a real kill switch file on disk.
        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.write_text("active", encoding="utf-8")

        orch_config = _make_orch_config(str(kill_file))
        alert_calls = []

        def capture_alert(event_type, context):
            alert_calls.append((event_type, context))

        # Mock all the initialization steps so run_loop() gets past Phase 1
        # and into the main loop, where the REAL _check_kill_switch runs
        # against the REAL file on disk.
        with patch("orchestrator.loop.load_config", return_value=orch_config), \
             patch("orchestrator.loop.load_credentials", return_value=("k", "s")), \
             patch("orchestrator.loop._create_paper_client", return_value=MagicMock()), \
             patch("orchestrator.loop.load_strategy_config", return_value=MagicMock(short_window=10, long_window=30)), \
             patch("orchestrator.loop.load_risk_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_execution_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_state", return_value=StateData()), \
             patch("orchestrator.loop.run_safety_check", return_value={"total_positions": 0, "unprotected_count": 0}), \
             patch("orchestrator.loop.save_state"), \
             patch("orchestrator.loop.alerting.initialize"), \
             patch("orchestrator.loop.alerting.send_alert", side_effect=capture_alert), \
             patch("orchestrator.loop.signal.signal"):

            with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                with pytest.raises(SystemExit) as exc_info:
                    run_loop("orchestrator/config.yaml")

        # Verify exit code 1 (clean kill switch activation).
        assert exc_info.value.code == 1

        # Verify CRITICAL log with event="kill_switch_triggered".
        critical_records = [
            r for r in caplog.records
            if r.levelno == logging.CRITICAL
            and r.__dict__.get("event") == "kill_switch_triggered"
        ]
        assert len(critical_records) >= 1
        assert str(kill_file) in critical_records[0].__dict__.get("file_path", "")

        # Verify send_alert was called with "kill_switch_triggered".
        alert_events = [c[0] for c in alert_calls]
        assert "kill_switch_triggered" in alert_events

    def test_kill_switch_fires_before_any_cycle_runs(self, tmp_path):
        """
        The kill switch check happens BEFORE any trading logic —
        _run_cycle and _check_new_bar are never called.
        """
        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.write_text("", encoding="utf-8")

        orch_config = _make_orch_config(str(kill_file))

        with patch("orchestrator.loop.load_config", return_value=orch_config), \
             patch("orchestrator.loop.load_credentials", return_value=("k", "s")), \
             patch("orchestrator.loop._create_paper_client", return_value=MagicMock()), \
             patch("orchestrator.loop.load_strategy_config", return_value=MagicMock(short_window=10, long_window=30)), \
             patch("orchestrator.loop.load_risk_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_execution_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_state", return_value=StateData()), \
             patch("orchestrator.loop.run_safety_check", return_value={"total_positions": 0, "unprotected_count": 0}), \
             patch("orchestrator.loop.save_state"), \
             patch("orchestrator.loop.alerting.initialize"), \
             patch("orchestrator.loop.alerting.send_alert"), \
             patch("orchestrator.loop.signal.signal"), \
             patch("orchestrator.loop._check_new_bar") as mock_bar, \
             patch("orchestrator.loop._run_cycle") as mock_cycle:

            with pytest.raises(SystemExit):
                run_loop("orchestrator/config.yaml")

        # Neither new-bar check nor cycle execution were reached.
        mock_bar.assert_not_called()
        mock_cycle.assert_not_called()


# ===========================================================================
# Scenario 2: Kill switch detected during _interruptible_wait
# ===========================================================================


class TestKillSwitchDuringWait:
    """
    Integration test: kill switch file appears during _interruptible_wait
    and is detected before the full timeout elapses.
    """

    def test_interruptible_wait_returns_kill_switch_when_file_appears(self, tmp_path):
        """
        _interruptible_wait detects a kill switch file created during the
        wait period, returns "kill_switch" well before the full timeout.
        """
        kill_file_path = str(tmp_path / "KILL_SWITCH")
        shutdown_event = threading.Event()

        # Create the kill switch file after a short delay in a background thread.
        def create_file_after_delay():
            time.sleep(0.3)  # 300ms delay
            with open(kill_file_path, "w") as f:
                f.write("triggered")

        creator_thread = threading.Thread(target=create_file_after_delay)
        creator_thread.start()

        # Start the interruptible wait with a long timeout but short check interval.
        start = time.monotonic()
        result = _interruptible_wait(
            shutdown_event,
            timeout=10.0,       # Long timeout — we should NOT wait this long.
            kill_switch_file_path=kill_file_path,
            check_interval=0.1,  # Check every 100ms for fast detection.
        )
        elapsed = time.monotonic() - start

        creator_thread.join()

        # Should have detected the file and returned "kill_switch".
        assert result == "kill_switch"

        # Should have returned well before the 10s timeout (within ~1s).
        assert elapsed < 2.0, (
            f"Waited {elapsed:.2f}s — should have detected kill switch "
            f"within ~0.5s (file created at 0.3s + 0.1s check interval)"
        )

    def test_kill_switch_during_wait_triggers_shutdown_on_next_iteration(self, tmp_path):
        """
        After _interruptible_wait returns "kill_switch", the next top-of-loop
        iteration catches it and triggers _kill_switch_shutdown.

        This exercises the full wiring: interruptible_wait → loop back to top →
        _check_kill_switch → _kill_switch_shutdown.
        """
        kill_file = tmp_path / "KILL_SWITCH"
        orch_config = _make_orch_config(str(kill_file))

        call_count = [0]

        # On the first loop iteration: no kill switch file, new bar check
        # returns no new bar, so the loop enters _interruptible_wait.
        # We mock _interruptible_wait to create the file then return "kill_switch".
        # On the second loop iteration: _check_kill_switch finds the file → shutdown.
        def mock_interruptible_wait(event, timeout, path, check_interval=5.0):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate: file appeared during wait.
                kill_file.write_text("created during wait", encoding="utf-8")
                return "kill_switch"
            # Should never be called a second time (loop terminates).
            return "timeout"

        with patch("orchestrator.loop.load_config", return_value=orch_config), \
             patch("orchestrator.loop.load_credentials", return_value=("k", "s")), \
             patch("orchestrator.loop._create_paper_client", return_value=MagicMock()), \
             patch("orchestrator.loop.load_strategy_config", return_value=MagicMock(short_window=10, long_window=30)), \
             patch("orchestrator.loop.load_risk_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_execution_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_state", return_value=StateData()), \
             patch("orchestrator.loop.run_safety_check", return_value={"total_positions": 0, "unprotected_count": 0}), \
             patch("orchestrator.loop.save_state"), \
             patch("orchestrator.loop.alerting.initialize"), \
             patch("orchestrator.loop.alerting.send_alert"), \
             patch("orchestrator.loop.signal.signal"), \
             patch("orchestrator.loop._check_new_bar", return_value=(False, None)), \
             patch("orchestrator.loop._interruptible_wait", side_effect=mock_interruptible_wait), \
             patch("threading.Event.clear"):

            with pytest.raises(SystemExit) as exc_info:
                run_loop("orchestrator/config.yaml")

        # The loop should have exited via kill switch (code 1).
        assert exc_info.value.code == 1


# ===========================================================================
# Scenario 3: End-to-end alert delivery with mocked Telegram API
# ===========================================================================


class TestEndToEndAlertDelivery:
    """
    Integration test: a cycle produces a fill → send_alert fires →
    HTTP POST to Telegram API with expected content.
    """

    def test_order_filled_sends_telegram_http_post(self, tmp_path):
        """
        When a cycle produces an order fill, the alerting module sends an
        HTTP POST to the Telegram sendMessage endpoint with the correct
        message content (symbol, fill price, order ID).
        """
        import signal as sig_module

        # Set up alerting config: credentials present, alerting enabled.
        alerting_config_file = tmp_path / "alerting_config.yaml"
        alerting_config_file.write_text(
            "telegram_bot_token: test-token-abc\n"
            "telegram_chat_id: '12345'\n",
            encoding="utf-8",
        )

        orch_config = _make_orch_config(str(tmp_path / "NO_KILL_SWITCH"))

        # Track HTTP requests made by the alerting module.
        http_requests = []

        class MockResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"ok": true}'

        def capture_urlopen(request, timeout=None):
            http_requests.append(request)
            return MockResponse()

        # Set up signal handler capture and shutdown trigger.
        captured_handler = [None]

        def capture_signal(signum, handler):
            if signum == sig_module.SIGINT:
                captured_handler[0] = handler

        call_count = [0]

        def mock_event_wait(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1 and captured_handler[0]:
                captured_handler[0](sig_module.SIGINT, None)

        # Cycle returns a filled order.
        order_result = {
            "status": "filled",
            "entry_order_id": "ord-fill-999",
            "filled_quantity": 15,
            "fill_price": 175.50,
            "signal": "BUY",
            "symbol": "AAPL",
        }

        with patch("orchestrator.loop.load_config", return_value=orch_config), \
             patch("orchestrator.loop.load_credentials", return_value=("k", "s")), \
             patch("orchestrator.loop._create_paper_client", return_value=MagicMock()), \
             patch("orchestrator.loop.load_strategy_config", return_value=MagicMock(short_window=10, long_window=30)), \
             patch("orchestrator.loop.load_risk_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_execution_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_state", return_value=StateData()), \
             patch("orchestrator.loop.run_safety_check", return_value={"total_positions": 0, "unprotected_count": 0}), \
             patch("orchestrator.loop.save_state"), \
             patch("orchestrator.loop.signal.signal", side_effect=capture_signal), \
             patch("orchestrator.loop._check_kill_switch", return_value=False), \
             patch("orchestrator.loop._check_new_bar", return_value=(True, "2024-06-16")), \
             patch("orchestrator.loop._run_cycle", return_value=("filled", order_result)), \
             patch("threading.Event.wait", side_effect=mock_event_wait), \
             patch("threading.Event.clear"), \
             patch("alerting.sender.urllib.request.urlopen", side_effect=capture_urlopen):

            # Use real alerting initialization with our test config.
            # We initialize BEFORE run_loop and patch the internal initialize
            # call to be a no-op (so it doesn't overwrite with the default path).
            import alerting
            alerting.initialize(str(alerting_config_file))

            with patch("orchestrator.loop.alerting.initialize"):
                with pytest.raises(SystemExit) as exc_info:
                    run_loop("orchestrator/config.yaml")

        # Graceful shutdown (code 0).
        assert exc_info.value.code == 0

        # Verify HTTP requests were made to Telegram.
        assert len(http_requests) >= 1, (
            "Expected at least one HTTP POST to Telegram API"
        )

        # Find the order_filled request (look for one containing fill_price).
        filled_requests = []
        for req in http_requests:
            body = req.data.decode("utf-8") if req.data else ""
            if "175.5" in body or "Order Filled" in body:
                filled_requests.append(req)

        assert len(filled_requests) >= 1, (
            f"Expected an HTTP request containing fill info, got {len(http_requests)} "
            f"requests total"
        )

        # Verify the URL is the Telegram sendMessage endpoint.
        req = filled_requests[0]
        assert "api.telegram.org" in req.full_url
        assert "sendMessage" in req.full_url
        assert "test-token-abc" in req.full_url

        # Verify the body contains expected content.
        body = json.loads(req.data.decode("utf-8"))
        assert body["chat_id"] == "12345"
        assert "AAPL" in body["text"] or "175.5" in body["text"]


# ===========================================================================
# Scenario 4: Orchestrator continues when alerting.initialize() fails
# ===========================================================================


class TestAlertingInitFailureContinues:
    """
    Integration test: alerting.initialize() raises an unexpected exception,
    but run_loop() continues into its main iteration.
    """

    def test_startup_continues_with_warning_on_alerting_failure(self, tmp_path, caplog):
        """
        When alerting.initialize() raises, run_loop() logs a WARNING and
        continues into the main loop (doesn't crash at startup).
        """
        kill_file = tmp_path / "KILL_SWITCH"
        # Don't create it yet — let the loop run one iteration, then create
        # it to terminate the test cleanly.
        orch_config = _make_orch_config(str(kill_file))

        # alerting.initialize() will raise.
        def failing_init(*args, **kwargs):
            raise RuntimeError("Simulated alerting init failure")

        iteration_count = [0]

        def mock_check_kill_switch(path):
            iteration_count[0] += 1
            if iteration_count[0] == 1:
                # First iteration: no kill switch, let loop proceed.
                return False
            else:
                # Second iteration: trigger kill switch to terminate.
                return True

        with patch("orchestrator.loop.load_config", return_value=orch_config), \
             patch("orchestrator.loop.load_credentials", return_value=("k", "s")), \
             patch("orchestrator.loop._create_paper_client", return_value=MagicMock()), \
             patch("orchestrator.loop.load_strategy_config", return_value=MagicMock(short_window=10, long_window=30)), \
             patch("orchestrator.loop.load_risk_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_execution_config", return_value=MagicMock()), \
             patch("orchestrator.loop.load_state", return_value=StateData()), \
             patch("orchestrator.loop.run_safety_check", return_value={"total_positions": 0, "unprotected_count": 0}), \
             patch("orchestrator.loop.save_state"), \
             patch("orchestrator.loop.alerting.initialize", side_effect=failing_init), \
             patch("orchestrator.loop.alerting.send_alert"), \
             patch("orchestrator.loop.signal.signal"), \
             patch("orchestrator.loop._check_kill_switch", side_effect=mock_check_kill_switch), \
             patch("orchestrator.loop._check_new_bar", return_value=(False, None)), \
             patch("orchestrator.loop._interruptible_wait", return_value="timeout"), \
             patch("threading.Event.clear"):

            with caplog.at_level(logging.DEBUG, logger="orchestrator"):
                with pytest.raises(SystemExit) as exc_info:
                    run_loop("orchestrator/config.yaml")

        # The loop entered and terminated via kill switch (not crash).
        assert exc_info.value.code in (1, 2)

        # A WARNING about alerting init failure was logged.
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "alerting" in r.getMessage().lower()
            and "failed" in r.getMessage().lower()
        ]
        assert len(warning_records) >= 1, (
            "Expected a WARNING about alerting initialization failure"
        )

        # The loop ran at least one iteration (proving it didn't crash at init).
        assert iteration_count[0] >= 2, (
            f"Expected loop to run at least 2 iterations, got {iteration_count[0]}"
        )

    def test_send_alert_is_noop_when_init_failed(self, tmp_path):
        """
        After alerting.initialize() fails, subsequent send_alert() calls
        don't crash — they're safe no-ops because config remains None.
        """
        import alerting as alerting_module

        # Simulate a failed initialization by resetting the module config.
        original_config = alerting_module._config
        alerting_module._config = None

        try:
            # This should not raise — send_alert handles config=None gracefully.
            alerting_module.send_alert("order_filled", {
                "symbol": "AAPL",
                "fill_price": 100.0,
            })
            # If we get here without an exception, the test passes.
        finally:
            alerting_module._config = original_config
