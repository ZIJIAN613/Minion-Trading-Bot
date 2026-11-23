# alerting/telegram_bot.py
#
# Telegram Bot listener for remote kill switch control and status reporting.
# This is a STRETCH GOAL feature — disabled by default.
#
# Runs as a daemon thread, polling Telegram's getUpdates endpoint using
# long-polling.  Only accepts commands from the configured chat_id.
# Terminates itself after 5 consecutive polling failures.
#
# Zero dependencies on orchestrator internals — communicates via:
#   - kill_switch_file_path (string): path to create when /kill is received
#   - get_status_callback (callable): returns a dict with current loop state
#
# Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9

import json
import logging
import os
import threading
import urllib.request
import urllib.error

logger = logging.getLogger("alerting")

# Maximum consecutive polling failures before the thread self-terminates.
# After this many failures, the thread logs a WARNING and exits cleanly.
_MAX_CONSECUTIVE_FAILURES = 5

# Long-polling timeout for the getUpdates call (seconds).
# Telegram holds the connection open for up to this duration, then returns
# an empty result if no new messages arrived.  30s is a standard choice
# that balances responsiveness against connection overhead.
_POLL_TIMEOUT_SECONDS = 30

# HTTP timeout for sendMessage replies (seconds).  Shorter than polling
# because these are quick one-shot requests.
_REPLY_TIMEOUT_SECONDS = 10

# Help message shown for unrecognized commands.
_HELP_MESSAGE = (
    "Supported commands:\n"
    "/kill — Activate the kill switch (halt trading immediately)\n"
    "/status — Show current loop state"
)


class TelegramBotListener:
    """
    Daemon thread that listens for /kill and /status commands via Telegram.

    Runs in a separate thread from the main Orchestrator loop, so command
    processing never interferes with trading cycle timing.  The thread is
    marked as a daemon, so it does not prevent the process from exiting.

    Only accepts commands from the configured chat_id — messages from other
    chat IDs are silently ignored (no reply, no log).

    Terminates itself after 5 consecutive polling failures (network errors,
    HTTP errors).  This is a safety net: if Telegram is unreachable for an
    extended period, the thread exits gracefully rather than spinning forever
    on failed requests.

    This class has ZERO direct dependencies on orchestrator internals.
    It communicates via:
      - kill_switch_file_path: a string path to create a file at
      - get_status_callback: a callable returning a status dict

    Args:
        bot_token:             Telegram bot API token (from @BotFather).
        chat_id:               The ONLY authorized chat ID.  Commands from
                               any other chat are silently ignored.
        kill_switch_file_path: File path to create when /kill is received.
        get_status_callback:   Callable returning a dict with keys like:
                               "running", "cycle_number", "last_outcome",
                               "kill_switch_active".
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        kill_switch_file_path: str,
        get_status_callback,
    ):
        self._bot_token = bot_token
        self._chat_id = str(chat_id)  # Ensure string for comparison.
        self._kill_switch_file_path = kill_switch_file_path
        self._get_status_callback = get_status_callback

        # threading.Event used to signal the polling loop to stop.
        # stop() sets this event, and the polling loop checks it between polls.
        self._stop_event = threading.Event()

        # The daemon thread itself — created in start(), None until then.
        self._thread = None

        # The Telegram API base URL for this bot.
        self._api_base = f"https://api.telegram.org/bot{self._bot_token}"

        # Offset for getUpdates — Telegram uses this to track which messages
        # have already been processed.  Set to 0 initially (get all pending),
        # then updated to last_update_id + 1 after each batch.
        self._offset = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the listener daemon thread.

        The thread runs until stop() is called or 5 consecutive polling
        failures occur.  Being a daemon thread, it does not prevent the
        process from exiting when the main thread terminates.
        """
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="TelegramBotListener",
            daemon=True,  # Does not block process exit.
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Signal the listener to stop cleanly.

        Sets the internal stop event, which causes the polling loop to
        exit after the current getUpdates call completes (or times out).
        Called during orchestrator shutdown.
        """
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal: Polling Loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """
        Main polling loop.  Runs in the daemon thread.

        Continuously calls getUpdates with long-polling.  Processes any
        incoming messages, then loops.  Exits when:
          - self._stop_event is set (clean shutdown), or
          - 5 consecutive polling failures occur (self-termination).
        """
        consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                updates = self._get_updates()
                consecutive_failures = 0  # Reset on success.

                for update in updates:
                    self._handle_update(update)

            except Exception as exc:
                consecutive_failures += 1

                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        f"Telegram bot listener: {consecutive_failures} consecutive "
                        f"polling failures — terminating thread. "
                        f"Last error: {type(exc).__name__}: {str(exc)[:200]}",
                        extra={
                            "event": "telegram_bot_terminated",
                            "consecutive_failures": consecutive_failures,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:200],
                        },
                    )
                    return  # Exit the thread cleanly.

                # Log individual failures at DEBUG (not WARNING) to avoid
                # spamming logs during brief network blips.
                logger.debug(
                    f"Telegram bot polling failed ({consecutive_failures}/"
                    f"{_MAX_CONSECUTIVE_FAILURES}): "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )

    # ------------------------------------------------------------------
    # Internal: Telegram API Calls
    # ------------------------------------------------------------------

    def _get_updates(self) -> list:
        """
        Call Telegram's getUpdates endpoint with long-polling.

        Returns a list of update dicts.  Each update contains at minimum
        an "update_id" and optionally a "message" with "chat" and "text".

        Raises on HTTP errors or network failures (caller handles retry logic).
        """
        url = (
            f"{self._api_base}/getUpdates"
            f"?timeout={_POLL_TIMEOUT_SECONDS}"
            f"&offset={self._offset}"
        )

        # urllib.request.urlopen with timeout covers the HTTP overhead;
        # the Telegram server holds the connection for up to POLL_TIMEOUT
        # seconds waiting for new messages, so our HTTP timeout must be
        # longer than the poll timeout to avoid premature disconnection.
        request = urllib.request.Request(url)

        with urllib.request.urlopen(
            request, timeout=_POLL_TIMEOUT_SECONDS + 10
        ) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not data.get("ok", False):
            raise RuntimeError(
                f"Telegram API returned ok=false: {data.get('description', 'unknown')}"
            )

        results = data.get("result", [])

        # Update the offset so we don't re-process the same messages.
        # Offset = last update_id + 1 tells Telegram to only return newer updates.
        if results:
            last_update_id = results[-1].get("update_id", 0)
            self._offset = last_update_id + 1

        return results

    def _send_reply(self, text: str) -> None:
        """
        Send a text reply to the configured chat_id.

        Fire-and-forget: if sending fails, we log at DEBUG and move on.
        A failed reply is not a critical error — the bot listener should
        continue processing future commands.
        """
        url = f"{self._api_base}/sendMessage"
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
        }).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=_REPLY_TIMEOUT_SECONDS):
                pass
        except Exception as exc:
            # Reply failed — log at DEBUG and continue.
            logger.debug(
                f"Telegram bot: failed to send reply: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )

    # ------------------------------------------------------------------
    # Internal: Message Handling
    # ------------------------------------------------------------------

    def _handle_update(self, update: dict) -> None:
        """
        Process a single Telegram update.

        Extracts the message text and chat_id, validates authorization,
        then dispatches to the appropriate command handler.
        """
        message = update.get("message")
        if message is None:
            return  # Not a message update (e.g., edited_message, callback_query).

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = message.get("text", "").strip()

        # Authorization check: silently ignore messages from other chats.
        # No reply, no log — per Requirement 8.3.
        if chat_id != self._chat_id:
            return

        # Dispatch to command handlers.
        if text == "/kill":
            self._handle_kill_command()
        elif text == "/status":
            self._handle_status_command()
        elif text.startswith("/"):
            # Unrecognized command — reply with help.
            self._send_reply(_HELP_MESSAGE)
        # Non-command messages (no leading /) are silently ignored.

    def _handle_kill_command(self) -> None:
        """
        Handle the /kill command: create the kill switch file.

        If the file already exists, reply indicating it's already active.
        If creation fails (e.g., permission error), reply with the error.
        """
        # Check if kill switch is already active.
        if os.path.exists(self._kill_switch_file_path):
            self._send_reply(
                "\u26a0\ufe0f Kill switch is already active.\n"
                f"File: {self._kill_switch_file_path}\n"
                "The bot will halt at the start of the next cycle."
            )
            return

        # Attempt to create the kill switch file.
        try:
            # Create parent directories if they don't exist.
            # os.makedirs with exist_ok=True is safe if the directory
            # already exists — like mkdir -p in shell.
            parent_dir = os.path.dirname(self._kill_switch_file_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            # Write the file.  Content is irrelevant (only existence matters),
            # but we include a timestamp for human readability.
            with open(self._kill_switch_file_path, "w", encoding="utf-8") as f:
                f.write("Kill switch activated via Telegram /kill command.\n")

            self._send_reply(
                "\U0001f534 Kill switch activated.\n"
                f"File created: {self._kill_switch_file_path}\n"
                "The bot will halt at the start of the next cycle."
            )

        except OSError as exc:
            # File creation failed — reply with the error.
            self._send_reply(
                "\u274c Failed to create kill switch file.\n"
                f"Error: {type(exc).__name__}: {str(exc)[:200]}\n"
                f"Path: {self._kill_switch_file_path}"
            )

    def _handle_status_command(self) -> None:
        """
        Handle the /status command: report current loop state.

        Calls get_status_callback() to get the current state, then formats
        and sends a reply.
        """
        try:
            status = self._get_status_callback()
        except Exception as exc:
            self._send_reply(
                "\u274c Failed to get loop status.\n"
                f"Error: {type(exc).__name__}: {str(exc)[:200]}"
            )
            return

        # Format the status dict into a human-readable message.
        running = status.get("running", "unknown")
        cycle_number = status.get("cycle_number", "N/A")
        last_outcome = status.get("last_outcome", "N/A")
        kill_switch_active = os.path.exists(self._kill_switch_file_path)

        # Use emoji indicators for quick visual parsing on mobile.
        running_indicator = "\U0001f7e2" if running else "\U0001f534"  # 🟢 or 🔴
        ks_indicator = "\U0001f534 ACTIVE" if kill_switch_active else "\U0001f7e2 inactive"

        reply = (
            f"Loop: {running_indicator} {'Running' if running else 'Stopped'}\n"
            f"Last cycle: #{cycle_number}\n"
            f"Last outcome: {last_outcome}\n"
            f"Kill switch: {ks_indicator}"
        )

        self._send_reply(reply)
