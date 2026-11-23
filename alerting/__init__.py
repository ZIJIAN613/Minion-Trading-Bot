# alerting/__init__.py
#
# Public API for the Telegram alerting module.
#
# This module exposes:
#   - send_alert(event_type, context): fire-and-forget Telegram notification
#   - initialize(): load config, optionally start the Telegram bot listener
#   - stop_bot(): stop the Telegram bot listener thread (safe if never started)
#
# Usage:
#     from alerting import initialize, send_alert, stop_bot
#
#     initialize(
#         kill_switch_file_path="orchestrator/KILL_SWITCH",
#         status_callback=lambda: {"running": True, ...},
#     )
#     send_alert("order_filled", {"symbol": "AAPL", "fill_price": 150.25, ...})
#     stop_bot()  # During shutdown
#
# If initialize() is never called, send_alert() still works safely — it
# delegates to sender.send_alert which handles config=None by returning
# immediately (no-op).
#
# Requirements: 3.5, 4.2, 5.1, 8.4, 8.6

"""
alerting/ — Telegram notification module for the Minions paper-trading bot.

Exposes a single fire-and-forget function: send_alert(event_type, context).
This function NEVER raises exceptions and NEVER blocks for more than 10 seconds.

Usage:
    from alerting import initialize, send_alert, stop_bot

    initialize(
        kill_switch_file_path="orchestrator/KILL_SWITCH",
        status_callback=lambda: {"running": True, ...},
    )
    send_alert("order_filled", {"symbol": "AAPL", "fill_price": 150.25, ...})
    stop_bot()  # During shutdown
"""

import logging

from alerting.config_loader import AlertingConfig, load_config, VALID_EVENT_TYPES

# Import the sender's send_alert with an underscore alias to avoid a name
# collision with this module's own public send_alert() function defined below.
# The public send_alert() is a thin wrapper that injects the module-level config.
from alerting.sender import send_alert as _send_alert

# Import the Telegram bot listener (stretch goal).
from alerting.telegram_bot import TelegramBotListener

logger = logging.getLogger("alerting")


# ---------------------------------------------------------------------------
# Module-level state — set by initialize(), read by send_alert()/stop_bot().
# ---------------------------------------------------------------------------

# AlertingConfig instance — set by initialize().
_config: AlertingConfig = None

# TelegramBotListener instance — set by initialize() if bot is enabled.
# None if the bot feature is disabled or if initialization didn't start it.
_bot_listener: TelegramBotListener = None


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

def initialize(
    config_path: str = "alerting/config.yaml",
    kill_switch_file_path: str = None,
    status_callback=None,
) -> None:
    """
    Load alerting configuration, log status, and optionally start the
    Telegram bot listener (stretch goal).

    Must be called once during Orchestrator initialization.  After this call,
    send_alert() will use the loaded config to determine whether to dispatch
    Telegram messages.

    If telegram_bot_enabled is True in the config AND both credentials are
    set AND kill_switch_file_path and status_callback are provided, the
    TelegramBotListener daemon thread is started.

    Args:
        config_path:           Path to alerting/config.yaml (default).
        kill_switch_file_path: Path to the kill switch file.  Required for
                               the Telegram bot to handle /kill commands.
                               If None and bot is enabled, a WARNING is logged
                               and the bot is not started.
        status_callback:       Callable returning a dict with loop status.
                               Required for the bot's /status command.
                               If None and bot is enabled, a WARNING is logged
                               and the bot is not started.
    """
    global _config, _bot_listener

    _config = load_config(config_path)

    # --- Telegram Bot Listener (stretch goal) ---
    # Only start if explicitly enabled AND credentials are configured.
    if _config.telegram_bot_enabled and _config.alerting_enabled:
        # Validate that required parameters were provided by the caller.
        if kill_switch_file_path is None:
            logger.warning(
                "Telegram bot is enabled in config but kill_switch_file_path "
                "was not provided to initialize(). Bot listener not started."
            )
        elif status_callback is None:
            logger.warning(
                "Telegram bot is enabled in config but status_callback "
                "was not provided to initialize(). Bot listener not started."
            )
        else:
            # All requirements met — start the bot listener.
            _bot_listener = TelegramBotListener(
                bot_token=_config.telegram_bot_token,
                chat_id=_config.telegram_chat_id,
                kill_switch_file_path=kill_switch_file_path,
                get_status_callback=status_callback,
            )
            _bot_listener.start()
            logger.info(
                "Telegram bot listener started.",
                extra={"event": "telegram_bot_started"},
            )


# ---------------------------------------------------------------------------
# send_alert (public API)
# ---------------------------------------------------------------------------

def send_alert(event_type: str, context: dict, *, _suppress_logging: bool = False) -> None:
    """
    Public API: dispatch a Telegram alert.  Fire-and-forget.

    Delegates to alerting.sender.send_alert with the module-level config.
    If initialize() was never called (config is None), the underlying
    sender returns immediately — no crash, no exception.

    This function NEVER raises under any circumstances.

    Args:
        event_type: One of the six recognized Alert_Event strings
                    (e.g., "order_filled", "kill_switch_triggered").
        context:    Dict of context values for the message (may be
                    incomplete — missing fields are substituted with "N/A").
        _suppress_logging: Keyword-only. Forwarded to the underlying sender to
                    skip internal logging on the double-failure path (see
                    alerting.sender.send_alert). Normal callers omit this.

    Returns:
        None (always).
    """
    _send_alert(event_type, context, config=_config, _suppress_logging=_suppress_logging)


# ---------------------------------------------------------------------------
# stop_bot (public API)
# ---------------------------------------------------------------------------

def stop_bot() -> None:
    """
    Stop the Telegram bot listener thread, if it was started.

    Safe to call even if the bot was never started (no-op in that case).
    Should be called during Orchestrator shutdown for clean thread cleanup.
    """
    global _bot_listener
    if _bot_listener is not None:
        _bot_listener.stop()
        _bot_listener = None


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------
__all__ = ["initialize", "send_alert", "stop_bot", "VALID_EVENT_TYPES"]
