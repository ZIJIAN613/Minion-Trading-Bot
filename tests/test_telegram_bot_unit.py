"""
tests/test_telegram_bot_unit.py — Unit tests for TelegramBotListener.

Covers:
  1. /kill command: creates file, already-active reply, creation failure
  2. /status command: normal status, callback failure
  3. Authorization: configured chat_id only, unauthorized /kill ignored
  4. Unrecognized command: help message reply
  5. Polling failure resilience: 5 consecutive failures → thread exits
  6. Daemon thread attribute

All HTTP calls are mocked — no real Telegram API requests.

Validates: Requirements 8.1, 8.2, 8.3, 8.5, 8.7, 8.8, 8.9

Run with:  python -m pytest tests/test_telegram_bot_unit.py -v
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from alerting.telegram_bot import TelegramBotListener


# ===========================================================================
# Helpers
# ===========================================================================


def _make_bot(tmp_path, chat_id="12345", status_callback=None):
    """Create a TelegramBotListener with a temp kill switch path."""
    kill_path = str(tmp_path / "KILL_SWITCH")
    if status_callback is None:
        status_callback = lambda: {
            "running": True,
            "cycle_number": 7,
            "last_outcome": "filled",
        }
    return TelegramBotListener(
        bot_token="test-token-abc",
        chat_id=chat_id,
        kill_switch_file_path=kill_path,
        get_status_callback=status_callback,
    )


def _make_update(chat_id, text, update_id=1):
    """Build a Telegram update dict simulating a message."""
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


# ===========================================================================
# 1. /kill command tests
# ===========================================================================


class TestKillCommand:
    """/kill creates the kill switch file and sends appropriate replies."""

    def test_kill_creates_file_and_sends_confirmation(self, tmp_path):
        """
        /kill from authorized chat → file created, confirmation reply sent.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        update = _make_update("12345", "/kill")

        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        # File should exist on disk.
        assert os.path.exists(bot._kill_switch_file_path)

        # A confirmation reply should have been sent.
        assert len(replies) == 1
        assert "activated" in replies[0]["text"].lower() or "kill switch" in replies[0]["text"].lower()

    def test_kill_already_active_sends_already_active_reply(self, tmp_path):
        """
        /kill when file already exists → reply indicates already active,
        file is not recreated.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        # Pre-create the kill switch file.
        with open(bot._kill_switch_file_path, "w") as f:
            f.write("pre-existing")

        update = _make_update("12345", "/kill")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        # Reply should mention "already active".
        assert len(replies) == 1
        assert "already" in replies[0]["text"].lower()

        # File content unchanged (not overwritten).
        with open(bot._kill_switch_file_path) as f:
            assert f.read() == "pre-existing"

    def test_kill_creation_failure_sends_error_reply(self, tmp_path):
        """
        /kill when file creation fails (OSError) → error reply sent,
        no exception propagates.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        update = _make_update("12345", "/kill")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        # Mock open() to raise OSError when trying to create the file.
        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            with patch("builtins.open", side_effect=OSError("Permission denied")):
                # Should not raise — error is handled internally.
                bot._handle_update(update)

        # Error reply should mention the failure.
        assert len(replies) == 1
        assert "failed" in replies[0]["text"].lower() or "error" in replies[0]["text"].lower()


# ===========================================================================
# 2. /status command tests
# ===========================================================================


class TestStatusCommand:
    """/status reports loop state from the callback."""

    def test_status_returns_formatted_state(self, tmp_path):
        """
        /status with a normal callback → reply contains state fields.
        """
        def status_cb():
            return {
                "running": True,
                "cycle_number": 42,
                "last_outcome": "filled",
            }

        bot = _make_bot(tmp_path, chat_id="999", status_callback=status_cb)
        update = _make_update("999", "/status")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        assert len(replies) == 1
        text = replies[0]["text"]
        assert "42" in text
        assert "filled" in text

    def test_status_callback_exception_sends_error_reply(self, tmp_path):
        """
        /status when callback raises → error reply, no crash.
        """
        def bad_cb():
            raise RuntimeError("loop state unavailable")

        bot = _make_bot(tmp_path, chat_id="999", status_callback=bad_cb)
        update = _make_update("999", "/status")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        assert len(replies) == 1
        assert "failed" in replies[0]["text"].lower() or "error" in replies[0]["text"].lower()


# ===========================================================================
# 3. Authorization tests
# ===========================================================================


class TestAuthorization:
    """Only the configured chat_id can issue commands."""

    def test_authorized_chat_id_processes_command(self, tmp_path):
        """
        Message from configured chat_id → command is processed.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        update = _make_update("12345", "/status")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        # Reply was sent (command processed).
        assert len(replies) == 1

    def test_unauthorized_chat_id_ignored_silently(self, tmp_path):
        """
        Message from a different chat_id → no reply, no action.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        # Message from chat_id "99999" — not authorized.
        update = _make_update("99999", "/status")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        # No reply sent.
        assert len(replies) == 0

    def test_unauthorized_kill_never_creates_file(self, tmp_path):
        """
        /kill from an unauthorized chat_id MUST NEVER create the kill switch
        file — this is the highest-stakes authorization check.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        # Attacker sends /kill from a different chat.
        update = _make_update("66666", "/kill")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        # File must NOT exist.
        assert not os.path.exists(bot._kill_switch_file_path)
        # No reply sent (silently ignored).
        assert len(replies) == 0


# ===========================================================================
# 4. Unrecognized command test
# ===========================================================================


class TestUnrecognizedCommand:
    """Unknown commands get a help message reply."""

    def test_unknown_command_sends_help(self, tmp_path):
        """
        /unknown_cmd → reply with help listing /kill and /status.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        update = _make_update("12345", "/restart")
        replies = []

        def mock_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            replies.append(body)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("alerting.telegram_bot.urllib.request.urlopen", side_effect=mock_urlopen):
            bot._handle_update(update)

        assert len(replies) == 1
        text = replies[0]["text"]
        assert "/kill" in text
        assert "/status" in text


# ===========================================================================
# 5. Polling failure resilience test
# ===========================================================================


class TestPollingResilience:
    """5 consecutive failures → thread exits cleanly with WARNING log."""

    def test_five_failures_terminates_loop(self, tmp_path, caplog):
        """
        When _get_updates raises 5 times in a row, the polling loop exits
        and logs a WARNING. The thread does not crash the process.
        """
        import logging

        bot = _make_bot(tmp_path, chat_id="12345")
        call_count = [0]

        def failing_get_updates():
            call_count[0] += 1
            raise ConnectionError(f"Network unreachable (attempt {call_count[0]})")

        # Replace _get_updates with our failing version.
        bot._get_updates = failing_get_updates

        # Run the poll loop directly (not in a thread, for deterministic testing).
        with caplog.at_level(logging.DEBUG, logger="alerting"):
            bot._poll_loop()

        # Should have attempted exactly 5 times then exited.
        assert call_count[0] == 5

        # A WARNING should have been logged about termination.
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) >= 1
        assert "terminat" in warning_records[0].getMessage().lower()

    def test_success_resets_failure_counter(self, tmp_path):
        """
        A successful poll between failures resets the counter — it takes
        5 CONSECUTIVE failures to terminate, not 5 total.
        """
        bot = _make_bot(tmp_path, chat_id="12345")
        call_count = [0]

        def intermittent_get_updates():
            call_count[0] += 1
            # Fail 4 times, succeed once, fail 4 times, succeed once, etc.
            # Should never reach 5 consecutive → loop never self-terminates.
            if call_count[0] % 5 == 0:
                return []  # Success (empty updates).
            if call_count[0] > 20:
                # Safety limit — stop the loop after 20 calls.
                bot._stop_event.set()
                return []
            raise ConnectionError("intermittent failure")

        bot._get_updates = intermittent_get_updates
        bot._poll_loop()

        # Loop ran more than 5 iterations (didn't self-terminate at 5).
        assert call_count[0] > 5


# ===========================================================================
# 6. Daemon thread test
# ===========================================================================


class TestDaemonThread:
    """The listener thread has daemon=True."""

    def test_thread_is_daemon(self, tmp_path):
        """
        After .start(), the underlying thread has daemon=True so it never
        blocks process exit.
        """
        bot = _make_bot(tmp_path, chat_id="12345")

        # Mock _poll_loop to exit immediately (don't actually poll).
        bot._poll_loop = lambda: None

        bot.start()

        assert bot._thread is not None
        assert bot._thread.daemon is True

        # Clean up — wait for the thread to finish (it returns immediately).
        bot._thread.join(timeout=1.0)
