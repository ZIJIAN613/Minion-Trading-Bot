# tests/test_execution_unit.py
#
# Example-based unit tests for the execution module.
# These tests cover specific scenarios and edge cases that complement
# the property-based tests in test_execution_properties.py.
#
# Organization:
#   - Config edge cases (Task 1.3)
#   - Paper safety + credential handling (Task 3.3)
#   - Bracket fallback (Task 5.4)
#   - Polling scenarios (Task 6.2)
#   - Error handling + logging (Task 7.5)

import io
import logging
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import yaml

from execution.config_loader import ExecutionConfig, load_config
from execution.execute import execute_decision


# ===========================================================================
# FIXTURES — shared test setup (like a C++ test fixture class)
# ===========================================================================
# @pytest.fixture decorates a function so pytest automatically calls it
# and passes the return value to any test that has a parameter with the
# same name.  This is dependency injection — like SetUp() in Google Test.

@pytest.fixture
def config():
    """A valid ExecutionConfig with short timeout for fast tests."""
    return ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=5)


@pytest.fixture
def approved_buy_dict():
    """A valid APPROVED BUY Decision_Dict."""
    return {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": pd.Timestamp("2024-06-15 12:00:00", tz="UTC"),
        "reason": "Buy signal from strategy",
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
        "timestamp": pd.Timestamp("2024-06-15 12:00:00", tz="UTC"),
        "reason": "Sell signal from strategy",
        "position_size": 10,
        "stop_loss": 145.0,
        "symbol": "AAPL",
    }


# ===========================================================================
# Task 1.3: Config edge cases
# ===========================================================================

class TestConfigEdgeCases:
    """Unit tests for config_loader edge cases (Task 1.3)."""

    def test_config_file_not_found(self):
        """
        Calling load_config with a non-existent path raises FileNotFoundError.
        Requirement 8.3.
        """
        with pytest.raises(FileNotFoundError) as exc_info:
            load_config("/nonexistent/path/config.yaml")

        # The error message should mention the path.
        assert "nonexistent" in str(exc_info.value)

    def test_config_invalid_yaml_syntax(self, tmp_path):
        """
        Invalid YAML syntax in the config file raises ValueError.
        Requirement 8.4.
        """
        # Write invalid YAML: a colon after an unquoted colon causes a parse error.
        config_file = tmp_path / "config.yaml"
        config_file.write_text("fill_timeout_seconds: :\n  bad: [unclosed", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(str(config_file))

        # Error message should reference YAML or the file path.
        assert "yaml" in str(exc_info.value).lower() or str(config_file) in str(exc_info.value)

    def test_config_loads_default_values(self):
        """
        Loading execution/config.yaml returns the expected defaults: 30.0 and 1.0.
        Requirement 8.1.
        """
        import os

        # Locate execution/config.yaml relative to this file.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(project_root, "execution", "config.yaml")

        config = load_config(config_path)

        assert config.fill_timeout_seconds == 30.0
        assert config.poll_interval_seconds == 1.0

    def test_config_unknown_keys_warning(self, tmp_path, caplog):
        """
        Unknown keys in config produce a WARNING log but no error.
        Requirement 8.8.

        caplog is a pytest fixture that captures log output — like a
        test spy on the logging system.
        """
        config_dict = {
            "fill_timeout_seconds": 30,
            "poll_interval_seconds": 1.0,
            "unknown_extra_key": "surprise",
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_dict), encoding="utf-8")

        # caplog.at_level() captures log messages at the specified level
        # or above from the specified logger.
        with caplog.at_level(logging.WARNING, logger="execution"):
            result = load_config(str(config_file))

        # Config should still load successfully.
        assert result.fill_timeout_seconds == 30.0
        assert result.poll_interval_seconds == 1.0

        # A WARNING log should mention the unknown key.
        assert any("unknown_extra_key" in record.message for record in caplog.records), (
            "Expected a WARNING log mentioning 'unknown_extra_key'"
        )


# ===========================================================================
# Task 3.3: Paper safety and credential handling
# ===========================================================================

class TestPaperSafetyAndCredentials:
    """Unit tests for paper-trading safety and credential propagation."""

    def test_paper_safety_base_url_check(self):
        """
        If the TradingClient has a non-paper base URL, RuntimeError is raised.
        Requirement 6.2.
        """
        # We patch _create_paper_client indirectly by patching the TradingClient
        # and load_credentials to avoid needing real credentials.
        with patch("execution.execute.load_credentials") as mock_creds, \
             patch("execution.execute.TradingClient") as MockTradingClient:

            mock_creds.return_value = ("fake_key", "fake_secret")

            # Create a mock client whose base URL does NOT contain "paper".
            mock_instance = MagicMock()
            mock_instance._base_url = "https://api.alpaca.markets"
            mock_instance.base_url = "https://api.alpaca.markets"
            MockTradingClient.return_value = mock_instance

            # Import and call _create_paper_client directly.
            from execution.execute import _create_paper_client

            with pytest.raises(RuntimeError) as exc_info:
                _create_paper_client()

            assert "paper" in str(exc_info.value).lower()

    def test_credential_error_propagated(self, config, approved_sell_dict):
        """
        If load_credentials raises EnvironmentError, it is captured in the
        error result (since client creation happens inside the try/except
        wrapper in execute_decision).
        Requirement 9.4: _create_paper_client propagates the error — but
        execute_decision's error wrapper catches it and returns status="error".
        """
        with patch("execution.execute.load_credentials") as mock_creds:
            mock_creds.side_effect = EnvironmentError("APCA_API_KEY_ID not set")

            # Call execute_decision WITHOUT passing a client, so it tries
            # to create one internally (calling load_credentials).
            # The error is caught by the general except block and returned.
            result = execute_decision(approved_sell_dict, config, client=None)

            assert result["status"] == "error"
            assert "OSError" in result["error_type"] or "EnvironmentError" in result["error_type"]
            assert "APCA_API_KEY_ID" in result["reason"]

    def test_approved_hold_returns_skipped(self, config):
        """
        APPROVED+HOLD is contradictory — returns skipped with reason about HOLD.
        Requirement 3.6.
        """
        d = {
            "decision": "APPROVED",
            "signal": "HOLD",
            "timestamp": pd.Timestamp.now("UTC"),
            "reason": "Contradictory decision",
            "position_size": 10,
            "stop_loss": 150.0,
            "symbol": "AAPL",
        }

        mock_client = MagicMock()
        result = execute_decision(d, config, client=mock_client)

        assert result["status"] == "skipped"
        assert "HOLD" in result["reason"]
        mock_client.submit_order.assert_not_called()


# ===========================================================================
# Task 5.4: Bracket fallback and edge cases
# ===========================================================================

class TestBracketFallback:
    """Unit tests for the two-step buy path (Task 5.4)."""

    def test_stop_loss_submitted_after_entry_fill(self, config, approved_buy_dict):
        """
        When the entry fills, a separate GTC stop-loss is submitted.
        The result includes both order IDs.
        """
        mock_client = MagicMock()

        # First call: market buy entry succeeds.
        mock_entry_order = MagicMock()
        mock_entry_order.id = "entry-001"
        mock_entry_order.status = "new"

        # Second call: stop-loss order succeeds.
        mock_stop_order = MagicMock()
        mock_stop_order.id = "stop-001"

        mock_client.submit_order.side_effect = [
            mock_entry_order,   # Market buy
            mock_stop_order,    # GTC stop-loss
        ]

        # get_order_by_id: first call in _submit_buy_bracket's poll loop
        # returns "filled", then subsequent calls in _poll_fill also return filled.
        mock_filled = MagicMock()
        mock_filled.status = "filled"
        mock_filled.filled_qty = "10"
        mock_filled.filled_avg_price = "155.00"
        mock_filled.filled_at = None
        mock_filled.qty = "10"
        mock_client.get_order_by_id.return_value = mock_filled

        result = execute_decision(approved_buy_dict, config, client=mock_client)

        assert result["status"] == "filled"
        assert result["entry_order_id"] == "entry-001"
        assert result["stop_loss_order_id"] == "stop-001"

    def test_stop_loss_submission_fails_returns_error(self, config, approved_buy_dict):
        """
        Entry placed OK but stop-loss submission raises → status="error".
        Requirement 4.6.
        """
        mock_client = MagicMock()

        # First call: market buy entry succeeds.
        mock_entry_order = MagicMock()
        mock_entry_order.id = "entry-ok-001"

        # Second call: stop-loss submission fails.
        mock_client.submit_order.side_effect = [
            mock_entry_order,                        # Entry succeeds
            RuntimeError("Stop order rejected"),     # Stop-loss fails
        ]

        # get_order_by_id returns filled (entry is confirmed held).
        mock_filled = MagicMock()
        mock_filled.status = "filled"
        mock_filled.filled_qty = "10"
        mock_client.get_order_by_id.return_value = mock_filled

        result = execute_decision(approved_buy_dict, config, client=mock_client)

        # The stop-loss failure should propagate as an error result.
        assert result["status"] == "error"
        assert "Stop order rejected" in result["reason"]


# ===========================================================================
# Task 6.2: Polling scenarios
# ===========================================================================

class TestPollingScenarios:
    """Unit tests for fill polling logic (Task 6.2)."""

    def test_order_filled_result_structure(self, config, approved_sell_dict):
        """
        Mock immediate fill — verify all Order_Result fields are present.
        Requirement 5.2.
        """
        mock_client = MagicMock()

        # submit_order succeeds.
        mock_order = MagicMock()
        mock_order.id = "fill-order-001"
        mock_client.submit_order.return_value = mock_order

        # get_order_by_id returns filled immediately.
        mock_filled = MagicMock()
        mock_filled.status = "filled"
        mock_filled.filled_avg_price = "152.50"
        mock_filled.filled_qty = "10"
        mock_filled.filled_at = None  # Triggers UTC fallback
        mock_filled.qty = "10"
        mock_client.get_order_by_id.return_value = mock_filled

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        # Verify all Order_Result fields for filled status.
        assert result["status"] == "filled"
        assert result["decision"] == "APPROVED"
        assert result["signal"] == "SELL"
        assert result["symbol"] == "AAPL"
        assert result["position_size"] == 10
        assert isinstance(result["reason"], str)
        assert 1 <= len(result["reason"]) <= 500
        assert isinstance(result["timestamp"], pd.Timestamp)
        assert result["timestamp"].tzinfo is not None
        assert result["entry_order_id"] == "fill-order-001"
        assert result["stop_loss_order_id"] is None  # SELL has no stop
        assert result["fill_price"] == 152.50
        assert result["filled_quantity"] == 10
        assert isinstance(result["filled_at"], pd.Timestamp)
        assert result["filled_at"].tzinfo is not None

    def test_partial_fill_at_timeout(self, approved_sell_dict):
        """
        Partial fill at timeout: verify partial_fill result with cancelled_quantity.
        Requirement 5.3.
        """
        # Use very short timeout so the test runs quickly.
        short_config = ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=5)
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "partial-order-001"
        mock_client.submit_order.return_value = mock_order

        # get_order_by_id always returns partially_filled (simulates timeout).
        mock_partial = MagicMock()
        mock_partial.status = "partially_filled"
        mock_partial.filled_avg_price = "149.00"
        mock_partial.filled_qty = "6"
        mock_partial.qty = "10"
        mock_partial.filled_at = None
        mock_client.get_order_by_id.return_value = mock_partial

        result = execute_decision(approved_sell_dict, short_config, client=mock_client)

        assert result["status"] == "partial_fill"
        assert result["filled_quantity"] == 6
        assert result["cancelled_quantity"] == 4
        assert result["fill_price"] == 149.0
        assert result["entry_order_id"] == "partial-order-001"

    def test_order_cancelled_before_fill(self, config, approved_sell_dict):
        """
        Order cancelled by broker → verify terminal state returned.
        Requirement 5.4.
        """
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "cancel-order-001"
        mock_client.submit_order.return_value = mock_order

        # get_order_by_id returns cancelled immediately.
        mock_cancelled = MagicMock()
        mock_cancelled.status = "cancelled"
        mock_client.get_order_by_id.return_value = mock_cancelled

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        assert result["status"] == "cancelled"
        assert result["entry_order_id"] == "cancel-order-001"
        assert result["decision"] == "APPROVED"

    def test_order_timeout_cancel(self, approved_sell_dict):
        """
        Order never fills within timeout → verify timeout + cancel.
        Requirement 5.5.
        """
        short_config = ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=5)
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "timeout-order-001"
        mock_client.submit_order.return_value = mock_order

        # get_order_by_id always returns "new" (non-terminal).
        mock_new = MagicMock()
        mock_new.status = "new"
        mock_client.get_order_by_id.return_value = mock_new

        result = execute_decision(approved_sell_dict, short_config, client=mock_client)

        assert result["status"] == "timeout"
        assert result["entry_order_id"] == "timeout-order-001"

    def test_network_error_during_polling_requery(self, config, approved_sell_dict):
        """
        First get_order_by_id raises, second succeeds → polling continues.
        Requirement 12.3.
        """
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "retry-order-001"
        mock_client.submit_order.return_value = mock_order

        # First call raises (network error), second call returns filled.
        mock_filled = MagicMock()
        mock_filled.status = "filled"
        mock_filled.filled_avg_price = "151.00"
        mock_filled.filled_qty = "10"
        mock_filled.filled_at = None
        mock_filled.qty = "10"

        mock_client.get_order_by_id.side_effect = [
            ConnectionError("Network blip"),  # First attempt fails
            mock_filled,                       # Re-query succeeds
        ]

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        assert result["status"] == "filled"
        assert result["fill_price"] == 151.0
        assert result["filled_quantity"] == 10


# ===========================================================================
# Task 7.5: Error handling and logging
# ===========================================================================

class TestErrorHandlingAndLogging:
    """Unit tests for error handling and structured logging (Task 7.5)."""

    def test_http_error_during_submission(self, config, approved_sell_dict):
        """
        HTTP-like error during submit_order → error result returned.
        Requirement 12.1.
        """
        mock_client = MagicMock()

        # Simulate an HTTP error by raising an exception with a descriptive message.
        mock_client.submit_order.side_effect = Exception(
            "HTTP 403 Forbidden: insufficient permissions"
        )

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        assert result["status"] == "error"
        assert "Exception" in result["error_type"]
        assert "403" in result["reason"]

    def test_logging_failure_swallowed(self, config, approved_sell_dict, monkeypatch):
        """
        If the logger raises, execution continues via sys.stderr fallback.
        Requirement 7.9.
        """
        # Make the execution logger always raise on any log call.
        real_logger = logging.getLogger("execution")
        monkeypatch.setattr(real_logger, "info", MagicMock(side_effect=RuntimeError("log broken")))
        monkeypatch.setattr(real_logger, "warning", MagicMock(side_effect=RuntimeError("log broken")))
        monkeypatch.setattr(real_logger, "error", MagicMock(side_effect=RuntimeError("log broken")))

        # Capture stderr output.
        captured_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured_stderr)

        # Mock client to raise so we exercise the error logging path.
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = ValueError("test error for logging")

        result = execute_decision(approved_sell_dict, config, client=mock_client)

        # Execution should complete — not crash due to logging failure.
        assert result["status"] == "error"

        # Stderr should contain fallback messages.
        stderr_output = captured_stderr.getvalue()
        assert "LOGGING FALLBACK" in stderr_output

    def test_log_event_order_submitted(self, config, approved_sell_dict, caplog):
        """
        On successful submission, an "order_submitted" log event is emitted.
        Requirement 7.1.
        """
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "log-order-001"
        mock_client.submit_order.return_value = mock_order

        # Return filled immediately so the test completes.
        mock_filled = MagicMock()
        mock_filled.status = "filled"
        mock_filled.filled_avg_price = "150.00"
        mock_filled.filled_qty = "10"
        mock_filled.filled_at = None
        mock_filled.qty = "10"
        mock_client.get_order_by_id.return_value = mock_filled

        with caplog.at_level(logging.INFO, logger="execution"):
            execute_decision(approved_sell_dict, config, client=mock_client)

        # Verify "order_submitted" appears in the log messages.
        log_messages = " ".join(record.message for record in caplog.records)
        assert "order_submitted" in log_messages, (
            f"Expected 'order_submitted' in log output, got: {log_messages}"
        )

    def test_log_event_order_filled(self, config, approved_sell_dict, caplog):
        """
        On fill confirmation, an "order_filled" log event is emitted.
        Requirement 7.2.
        """
        mock_client = MagicMock()

        mock_order = MagicMock()
        mock_order.id = "log-fill-001"
        mock_client.submit_order.return_value = mock_order

        mock_filled = MagicMock()
        mock_filled.status = "filled"
        mock_filled.filled_avg_price = "150.00"
        mock_filled.filled_qty = "10"
        mock_filled.filled_at = None
        mock_filled.qty = "10"
        mock_client.get_order_by_id.return_value = mock_filled

        with caplog.at_level(logging.INFO, logger="execution"):
            execute_decision(approved_sell_dict, config, client=mock_client)

        log_messages = " ".join(record.message for record in caplog.records)
        assert "order_filled" in log_messages, (
            f"Expected 'order_filled' in log output, got: {log_messages}"
        )

    def test_log_event_decision_skipped(self, config, caplog):
        """
        VETOED decision produces a "decision_skipped" log event.
        Requirement 7.6.
        """
        d = {
            "decision": "VETOED",
            "signal": "BUY",
            "timestamp": pd.Timestamp.now("UTC"),
            "reason": "Risk engine vetoed the trade",
        }

        mock_client = MagicMock()

        with caplog.at_level(logging.INFO, logger="execution"):
            result = execute_decision(d, config, client=mock_client)

        assert result["status"] == "skipped"

        log_messages = " ".join(record.message for record in caplog.records)
        assert "decision_skipped" in log_messages, (
            f"Expected 'decision_skipped' in log output, got: {log_messages}"
        )
