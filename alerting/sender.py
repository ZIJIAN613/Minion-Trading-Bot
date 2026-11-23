# alerting/sender.py
#
# The core alerting function: formats messages and sends them to the Telegram
# Bot API.  This module exposes a single public function send_alert() that
# NEVER raises exceptions under any circumstances.
#
# Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.3, 4.5

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

# TYPE_CHECKING is a special constant that is True only when a type checker
# (like mypy) is analyzing the code — at runtime it is always False.
# This avoids a circular import: sender.py uses AlertingConfig in its type
# hints, but config_loader.py doesn't depend on sender.py.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alerting.config_loader import AlertingConfig

from alerting.config_loader import VALID_EVENT_TYPES

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("alerting")


# ---------------------------------------------------------------------------
# Module Constants
# ---------------------------------------------------------------------------

# Timeout for each Telegram API call (seconds).
# Covers the ENTIRE HTTP operation: DNS resolution + TCP connect + response
# read.  If the operation exceeds this, urllib raises socket.timeout (caught
# by our blanket except).
_HTTP_TIMEOUT_SECONDS = 5

# Maximum Telegram message length (characters).  The Telegram Bot API rejects
# messages longer than 4096 characters with HTTP 400.  We truncate before
# sending to prevent that rejection.
_MAX_MESSAGE_LENGTH = 4096

# Human-readable labels for each event type.
# Each label starts with an emoji for quick visual identification when
# glancing at Telegram notifications on a phone.
_EVENT_LABELS = {
    "order_submitted": "\U0001f4e4 Order Submitted",       # 📤
    "order_filled": "\u2705 Order Filled",                 # ✅
    "skip_and_retry_error": "\u26a0\ufe0f Cycle Skipped (Retry)",  # ⚠️
    "halt_the_loop_error": "\U0001f6d1 Loop Halted (Error)",       # 🛑
    "kill_switch_triggered": "\U0001f534 Kill Switch Triggered",   # 🔴
    "graceful_shutdown_completed": "\U0001f7e2 Graceful Shutdown",  # 🟢
}

# Required context fields per event type.
# When formatting a message, each field listed here will appear in the output.
# If a field is missing from the provided context dict, "N/A" is substituted.
_REQUIRED_CONTEXT = {
    "order_submitted": ["symbol", "order_id"],
    "order_filled": ["symbol", "order_id", "filled_quantity", "fill_price"],
    "skip_and_retry_error": ["error_summary", "cycle_number"],
    "halt_the_loop_error": ["error_summary", "symbol", "order_id"],
    "kill_switch_triggered": ["cycle_number"],
    "graceful_shutdown_completed": ["cycle_number"],
}


# ---------------------------------------------------------------------------
# _format_message
# ---------------------------------------------------------------------------

def _format_message(event_type: str, context: dict) -> str:
    """
    Format a Telegram alert message for the given event.

    Algorithm:
      1. Look up the human-readable emoji label for event_type.
      2. Generate UTC timestamp in ISO 8601 format.
      3. Build context lines: for each required field of this event_type,
         use context.get(field, "N/A") to substitute missing values.
      4. Assemble the final message: label + timestamp + context lines.
      5. Truncate to _MAX_MESSAGE_LENGTH characters.

    Args:
        event_type: One of the six valid Alert_Event strings.
        context:    Dict of context values (may be incomplete — missing
                    fields are substituted with "N/A").

    Returns:
        Formatted message string, guaranteed ≤ 4096 characters.
    """
    # Step 1: Look up the emoji label.
    # dict.get(key, default) — returns the default if key is absent.
    # This should never hit the default because we validate event_type
    # before calling _format_message(), but defensive coding costs nothing.
    label = _EVENT_LABELS.get(event_type, event_type)

    # Step 2: Generate UTC timestamp in ISO 8601 format.
    # datetime.now(timezone.utc) gives the current time in UTC — like
    # std::chrono::system_clock::now() in C++.
    # .isoformat() produces "2024-06-15T14:30:00+00:00".
    # We replace "+00:00" with "Z" for the shorter UTC notation.
    utc_now = datetime.now(timezone.utc)
    timestamp = utc_now.isoformat().replace("+00:00", "Z")

    # Step 3: Build context lines.
    # _REQUIRED_CONTEXT[event_type] is the list of field names for this event.
    required_fields = _REQUIRED_CONTEXT.get(event_type, [])

    # Build each context line as "Field Name: value" with title-cased labels.
    # str.replace("_", " ").title() converts "filled_quantity" → "Filled Quantity".
    context_lines = []
    for field in required_fields:
        # Get the value from the caller-provided context dict.
        # If the field is absent, substitute "N/A" (Requirement 3.2).
        value = context.get(field, "N/A")

        # Convert the value to a string for display.
        # str() is safe on any type — it won't raise.
        display_value = str(value)

        # Build a human-readable label from the field name:
        # "order_id" → "Order Id", "fill_price" → "Fill Price"
        field_label = field.replace("_", " ").title()

        context_lines.append(f"{field_label}: {display_value}")

    # Step 4: Assemble the final message.
    # Format: label on first line, timestamp on second, blank line, then
    # context fields each on their own line.
    # "\n".join(list) concatenates all items with newline separators —
    # like boost::algorithm::join in C++.
    context_block = "\n".join(context_lines)
    message = f"{label}\nTime: {timestamp}\n\n{context_block}"

    # Step 5: Truncate to the Telegram maximum.
    # Python string slicing s[:N] returns at most N characters — if the
    # string is shorter than N, it returns the whole string (no exception).
    if len(message) > _MAX_MESSAGE_LENGTH:
        message = message[:_MAX_MESSAGE_LENGTH]

    return message


# ---------------------------------------------------------------------------
# send_alert
# ---------------------------------------------------------------------------

def send_alert(
    event_type: str,
    context: dict,
    *,
    config: "AlertingConfig" = None,
    _suppress_logging: bool = False,
) -> None:
    """
    Dispatch a Telegram alert for the given event.  Fire-and-forget.

    This function NEVER raises — all exceptions are caught internally.
    It is safe to call from anywhere in the trading pipeline without
    worrying about error propagation.

    _suppress_logging (keyword-only, default False):
        When True, ALL internal logger.*() calls in this function are skipped.
        This exists for ONE caller: log_system's SafeFileHandler double-failure
        path (Requirement 1.4). If the logging system itself has failed on both
        file AND stderr, dispatching an alert must not re-enter the logging
        framework — otherwise we risk recursive failure. The HTTP dispatch still
        happens exactly as normal; only the logging is suppressed. Normal callers
        never pass this flag and behaviour is unchanged for them.

    Algorithm:
      1. If config is None or alerting is disabled → return immediately.
      2. If event_type is not in VALID_EVENT_TYPES → log WARNING, return.
      3. If event_type is not in config.enabled_events → return silently.
      4. Format the message via _format_message().
      5. Build the Telegram API URL and POST payload.
      6. Send HTTP POST with timeout=_HTTP_TIMEOUT_SECONDS.
      7. If any exception occurs → log WARNING with type and message
         (truncated to 200 chars), return None.

    Args:
        event_type: The alert event string (e.g., "order_filled").
        context:    Dict of context values for the message.
        config:     AlertingConfig instance (injected by the module-level
                    send_alert wrapper in alerting/__init__.py).

    Returns:
        None (always — never raises).
    """
    try:
        # ------------------------------------------------------------------
        # Step 1: Check if alerting is enabled.
        # ------------------------------------------------------------------
        if config is None:
            return None

        if not config.alerting_enabled:
            return None

        # ------------------------------------------------------------------
        # Step 2: Validate event_type against recognized set.
        # ------------------------------------------------------------------
        if event_type not in VALID_EVENT_TYPES:
            # Guard the log call behind _suppress_logging (see docstring): the
            # double-failure path must not re-enter the logging framework.
            if not _suppress_logging:
                logger.warning(
                    "Unrecognized event_type passed to send_alert: '%s'. "
                    "Skipping alert. Valid types are: %s",
                    event_type,
                    ", ".join(sorted(VALID_EVENT_TYPES)),
                )
            return None

        # ------------------------------------------------------------------
        # Step 3: Check if this event type is enabled in config.
        # ------------------------------------------------------------------
        if event_type not in config.enabled_events:
            return None

        # ------------------------------------------------------------------
        # Step 4: Format the message.
        # ------------------------------------------------------------------
        message = _format_message(event_type, context)

        # ------------------------------------------------------------------
        # Step 5: Build the Telegram API URL and POST payload.
        # ------------------------------------------------------------------
        # The Telegram Bot API URL format:
        #   https://api.telegram.org/bot<TOKEN>/sendMessage
        # The body is JSON with "chat_id" and "text" fields.
        url = (
            f"https://api.telegram.org/bot{config.telegram_bot_token}"
            f"/sendMessage"
        )

        # Build the JSON payload.
        # json.dumps() serializes a Python dict to a JSON string.
        payload = json.dumps({
            "chat_id": config.telegram_chat_id,
            "text": message,
        })

        # Encode to bytes for urllib — HTTP bodies must be bytes, not str.
        # .encode("utf-8") converts a Python str to bytes using UTF-8.
        payload_bytes = payload.encode("utf-8")

        # ------------------------------------------------------------------
        # Step 6: Send HTTP POST with timeout.
        # ------------------------------------------------------------------
        # urllib.request.Request builds an HTTP request object.
        # Setting method="POST" and data=payload_bytes makes it a POST.
        # The Content-Type header tells Telegram we're sending JSON.
        request = urllib.request.Request(
            url,
            data=payload_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        # urllib.request.urlopen() sends the request and returns a response.
        # timeout=_HTTP_TIMEOUT_SECONDS covers DNS + connect + read.
        # If anything takes longer, socket.timeout is raised (caught below).
        #
        # We use a 'with' block (context manager) to ensure the response
        # socket is closed promptly after reading — no resource leaks.
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS):
            # We don't need the response body — just confirming delivery.
            pass

        return None

    except Exception as exc:
        # ------------------------------------------------------------------
        # Step 7: Catch ALL exceptions and log a WARNING.
        # ------------------------------------------------------------------
        # This is the critical safety net: no matter what goes wrong
        # (network timeout, DNS failure, HTTP error, malformed URL,
        # unexpected runtime error, etc.), we log it and return None.
        #
        # We catch Exception (not BaseException) so that KeyboardInterrupt
        # and SystemExit can still propagate normally — they are subclasses
        # of BaseException but NOT Exception.
        #
        # type(exc).__name__ gives the exception class name as a string,
        # e.g., "TimeoutError", "URLError", "HTTPError".
        # str(exc)[:200] truncates the message to avoid log flooding.
        #
        # Guarded by _suppress_logging: when the logging system itself has
        # failed (double-failure path), we must NOT log here or we risk
        # re-entering the broken logging framework recursively.
        if not _suppress_logging:
            logger.warning(
                "Alert delivery failed for event '%s': [%s] %s",
                event_type,
                type(exc).__name__,
                str(exc)[:200],
            )
        return None
