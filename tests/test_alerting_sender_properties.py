"""
tests/test_alerting_sender_properties.py — Property-based tests for alerting/sender.py.

Four property groups covering the core guarantees of the alerting sender module:

  Property 2 — Message formatting includes required fields with N/A substitution
  Property 3 — Message truncation to Telegram maximum (4096 chars)
  Property 4 — send_alert never raises exceptions
  Property 5 — Unrecognized event_type prevents message delivery

Uses Hypothesis to generate random inputs and verify universal invariants.

Validates: Requirements 3.2, 3.3, 3.5, 3.10, 4.2

Run with:  python -m pytest tests/test_alerting_sender_properties.py -v
"""

import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from alerting.config_loader import AlertingConfig, VALID_EVENT_TYPES
from alerting.sender import (
    send_alert,
    _format_message,
    _EVENT_LABELS,
    _REQUIRED_CONTEXT,
    _MAX_MESSAGE_LENGTH,
)


# ===========================================================================
# Generator Strategies
# ===========================================================================

# Sorted list of valid event types for deterministic sampling.
_VALID_EVENTS_LIST = sorted(VALID_EVENT_TYPES)

# Strategy: a single valid event type.
valid_event_type = st.sampled_from(_VALID_EVENTS_LIST)

# Strategy: a string that is NOT a valid event type.
invalid_event_type = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P")),
    min_size=1,
    max_size=50,
).filter(lambda s: s not in VALID_EVENT_TYPES)

# Strategy: a context dict with an arbitrary subset of fields for a given
# event type.  Fields present have random string values; fields absent are
# simply omitted from the dict.
def context_for_event(event_type: str):
    """
    Build a Hypothesis strategy that generates context dicts for the given
    event type.  Each required field is independently present or absent,
    and present fields have random string values (1–100 chars).
    """
    required_fields = _REQUIRED_CONTEXT.get(event_type, [])
    # For each field, either include it with a random value or omit it.
    # st.fixed_dictionaries builds a dict with fixed keys; we combine with
    # st.just(None) and filter out None values to simulate optional presence.
    field_strategies = {}
    for field in required_fields:
        # Each field is either a random string or the sentinel "ABSENT"
        field_strategies[field] = st.one_of(
            st.text(min_size=1, max_size=100),
            st.none(),  # None means "field is absent from context"
        )
    return st.fixed_dictionaries(field_strategies).map(
        # Remove keys whose value is None — simulates the field being absent.
        lambda d: {k: v for k, v in d.items() if v is not None}
    )


# Strategy: context with very long string values (for truncation testing).
def big_context_for_event(event_type: str):
    """
    Build a Hypothesis strategy that generates context dicts where every
    required field has a very long string value (up to 100,000 chars).
    """
    required_fields = _REQUIRED_CONTEXT.get(event_type, [])
    field_strategies = {
        field: st.text(min_size=1000, max_size=100000)
        for field in required_fields
    }
    return st.fixed_dictionaries(field_strategies)


# Helper: create an enabled AlertingConfig for testing send_alert.
def _enabled_config(enabled_events=None):
    """Return an AlertingConfig with alerting enabled and all events active."""
    if enabled_events is None:
        enabled_events = list(VALID_EVENT_TYPES)
    return AlertingConfig(
        telegram_bot_token="test-token-abc",
        telegram_chat_id="123456",
        enabled_events=enabled_events,
        telegram_bot_enabled=False,
        alerting_enabled=True,
    )


# ===========================================================================
# Property 2: Alert message formatting includes required fields with N/A
#             substitution
# ===========================================================================
# Feature: kill-switch-alerting, Property 2: Alert message formatting includes
# required fields with N/A substitution


class TestMessageFormatting:
    """
    Property 2: For any valid event_type and any context dict (with an
    arbitrary subset of required fields present), the formatted message
    SHALL contain: (a) the human-readable event label, (b) a valid UTC
    ISO 8601 timestamp, and (c) for each required context field, either
    the provided value or "N/A" if missing.
    """

    @given(data=st.data())
    @settings(max_examples=200)
    def test_format_contains_label_timestamp_and_fields(self, data):
        """
        The formatted message includes the event label, a UTC timestamp,
        and every required field (with N/A substitution for missing ones).
        """
        # Draw a random valid event type.
        event_type = data.draw(valid_event_type)

        # Draw a context dict with random field presence.
        context = data.draw(context_for_event(event_type))

        # Format the message.
        message = _format_message(event_type, context)

        # (a) The message must contain the event label.
        expected_label = _EVENT_LABELS[event_type]
        assert expected_label in message, (
            f"Label '{expected_label}' not found in message"
        )

        # (b) The message must contain a UTC ISO 8601 timestamp.
        # Pattern matches: 2024-06-15T14:30:00.123456Z (with optional
        # fractional seconds).
        iso_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        assert re.search(iso_pattern, message), (
            "No ISO 8601 timestamp found in message"
        )

        # (c) For each required field, the message contains the value or "N/A".
        required_fields = _REQUIRED_CONTEXT[event_type]
        for field in required_fields:
            if field in context:
                # The provided value should appear in the message.
                assert str(context[field]) in message, (
                    f"Provided value for '{field}' not found in message"
                )
            else:
                # "N/A" should appear as the substitution.
                assert "N/A" in message, (
                    f"'N/A' substitution not found for missing field '{field}'"
                )


# ===========================================================================
# Property 3: Alert message truncation to Telegram maximum
# ===========================================================================
# Feature: kill-switch-alerting, Property 3: Alert message truncation to
# Telegram maximum


class TestMessageTruncation:
    """
    Property 3: For any valid event_type and any context dict containing
    string values of arbitrary length (up to 100,000 characters per field),
    the final formatted message SHALL be at most 4096 characters.
    """

    @given(data=st.data())
    @settings(max_examples=100)
    def test_message_never_exceeds_4096_chars(self, data):
        """
        Regardless of how long context field values are, the formatted
        message is always ≤ _MAX_MESSAGE_LENGTH (4096) characters.
        """
        event_type = data.draw(valid_event_type)
        context = data.draw(big_context_for_event(event_type))

        message = _format_message(event_type, context)

        assert len(message) <= _MAX_MESSAGE_LENGTH, (
            f"Message length {len(message)} exceeds maximum {_MAX_MESSAGE_LENGTH}"
        )


# ===========================================================================
# Property 4: send_alert never raises exceptions
# ===========================================================================
# Feature: kill-switch-alerting, Property 4: send_alert never raises exceptions


class TestSendAlertNeverRaises:
    """
    Property 4: For any input combination — valid or invalid event_type,
    any context dict, regardless of HTTP call outcome — send_alert() SHALL
    return None without propagating any exception.
    """

    @given(
        event_type=st.one_of(valid_event_type, invalid_event_type, st.text()),
        context=st.one_of(
            st.dictionaries(st.text(max_size=20), st.text(max_size=100)),
            st.just({}),
            st.just({"key": None}),
            st.just({"nested": {"deep": [1, 2, 3]}}),
        ),
    )
    @settings(max_examples=200)
    def test_send_alert_returns_none_on_http_success(self, event_type, context):
        """
        With a successful HTTP response (mocked), send_alert returns None.
        """
        config = _enabled_config()

        # Mock urlopen to simulate a successful HTTP response.
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("alerting.sender.urllib.request.urlopen", return_value=mock_response):
            result = send_alert(event_type, context, config=config)

        assert result is None

    @given(
        event_type=valid_event_type,
        exc_type=st.sampled_from([
            TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            ConnectionError,
            ConnectionRefusedError,
        ]),
    )
    @settings(max_examples=100)
    def test_send_alert_returns_none_on_http_failure(self, event_type, exc_type):
        """
        When urlopen raises any exception type, send_alert catches it and
        returns None without propagating.
        """
        config = _enabled_config()
        context = {"symbol": "AAPL", "order_id": "test-123"}

        with patch(
            "alerting.sender.urllib.request.urlopen",
            side_effect=exc_type("simulated failure"),
        ):
            result = send_alert(event_type, context, config=config)

        assert result is None

    @given(event_type=valid_event_type)
    @settings(max_examples=100)
    def test_send_alert_returns_none_on_url_error(self, event_type):
        """
        URLError (network unreachable, DNS failure) is caught gracefully.
        """
        config = _enabled_config()
        context = {"symbol": "SPY", "cycle_number": 42}

        with patch(
            "alerting.sender.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Name or service not known"),
        ):
            result = send_alert(event_type, context, config=config)

        assert result is None

    def test_send_alert_returns_none_with_none_config(self):
        """send_alert with config=None returns None immediately."""
        result = send_alert("order_filled", {"symbol": "X"}, config=None)
        assert result is None

    def test_send_alert_returns_none_with_disabled_config(self):
        """send_alert with alerting_enabled=False returns None immediately."""
        config = AlertingConfig(
            telegram_bot_token="",
            telegram_chat_id="",
            enabled_events=[],
            telegram_bot_enabled=False,
            alerting_enabled=False,
        )
        result = send_alert("order_filled", {"symbol": "X"}, config=config)
        assert result is None


# ===========================================================================
# Property 5: Unrecognized event_type prevents message delivery
# ===========================================================================
# Feature: kill-switch-alerting, Property 5: Unrecognized event_type prevents
# message delivery


class TestUnrecognizedEventPreventsDelivery:
    """
    Property 5: For any string that is not one of the six recognized
    Alert_Event types, calling send_alert() SHALL NOT result in any HTTP
    request to the Telegram API.
    """

    @given(event_type=invalid_event_type)
    @settings(max_examples=200)
    def test_no_http_request_for_invalid_event_type(self, event_type):
        """
        When event_type is not recognized, urlopen is never called.
        """
        config = _enabled_config()
        context = {"symbol": "TEST", "order_id": "xyz"}

        with patch("alerting.sender.urllib.request.urlopen") as mock_urlopen:
            result = send_alert(event_type, context, config=config)

        # urlopen must NOT have been called — no HTTP request made.
        mock_urlopen.assert_not_called()
        assert result is None
