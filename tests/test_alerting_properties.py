"""
tests/test_alerting_properties.py — Property-based tests for the alerting module.

# Feature: kill-switch-alerting, Property 6: enabled_events config validation
# retains only recognized values

Uses Hypothesis to verify that load_config() correctly filters the
enabled_events list: valid event types are preserved in order, and all
unrecognized strings are excluded.

Validates: Requirements 5.4

Run with:  python -m pytest tests/test_alerting_properties.py -v
"""

import os
import tempfile

import yaml
import pytest

# Hypothesis imports — see test_strategy_properties.py header for C++ analogy.
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from alerting.config_loader import (
    AlertingConfig,
    load_config,
    VALID_EVENT_TYPES,
)


# ===========================================================================
# Generator Strategies
# ===========================================================================

# A sorted list of the valid event types — sorted so Hypothesis can
# reproducibly sample from it.
_VALID_EVENTS_LIST = sorted(VALID_EVENT_TYPES)

# Strategy that generates a single valid event type string.
valid_event_type = st.sampled_from(_VALID_EVENTS_LIST)

# Strategy that generates a random string that is NOT a valid event type.
# We filter out any accidentally-generated valid strings.
invalid_event_type = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P")),
    min_size=1,
    max_size=50,
).filter(lambda s: s not in VALID_EVENT_TYPES)

# Strategy that generates a mixed list of valid and invalid event type strings.
# The list may be empty, contain duplicates, or contain only valid/invalid items.
mixed_event_list = st.lists(
    st.one_of(valid_event_type, invalid_event_type),
    min_size=0,
    max_size=20,
)


# ===========================================================================
# Property 6: enabled_events config validation retains only recognized values
# ===========================================================================
# Feature: kill-switch-alerting, Property 6: enabled_events config validation
# retains only recognized values


class TestEnabledEventsFiltering:
    """
    Property 6: For any list of strings provided as enabled_events in the
    config (containing a mix of valid event type names and arbitrary invalid
    strings), the resulting AlertingConfig.enabled_events SHALL contain
    exactly those strings from the input list that are members of the six
    recognized event types, preserving their order — all unrecognized
    strings SHALL be excluded.
    """

    @given(event_list=mixed_event_list)
    @settings(max_examples=200)  # Above the 100 minimum for confidence
    def test_enabled_events_retains_only_valid_preserving_order(
        self, event_list
    ):
        """
        Given an arbitrary mixed list of event type strings, load_config()
        returns only the valid ones in their original order.
        """
        # Build a minimal valid config file with the generated enabled_events.
        config_data = {
            "telegram_bot_token": "test-token-123",
            "telegram_chat_id": "12345",
            "enabled_events": event_list,
        }

        # Write config to a temp file using tempfile (not tmp_path fixture,
        # which is function-scoped and conflicts with Hypothesis).
        # tempfile.NamedTemporaryFile with delete=False gives us a path we
        # can pass to load_config, then we clean up manually.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(config_data, f, default_flow_style=False)
            config_path = f.name

        try:
            # Load the config.
            cfg = load_config(config_path)

            # Compute the expected result: filter the input list to keep only
            # strings that are in VALID_EVENT_TYPES, preserving order.
            # This is a LIST COMPREHENSION — Python's equivalent of a C++
            # std::copy_if into a new vector with a predicate lambda.
            expected = [e for e in event_list if e in VALID_EVENT_TYPES]

            # Assert the loaded config matches the expected filtered list.
            assert cfg.enabled_events == expected
        finally:
            os.unlink(config_path)

    @given(event_list=st.lists(valid_event_type, min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_all_valid_events_preserved(self, event_list):
        """
        When all items in enabled_events are valid, every item is preserved.
        """
        config_data = {
            "telegram_bot_token": "token",
            "telegram_chat_id": "chat",
            "enabled_events": event_list,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(config_data, f, default_flow_style=False)
            config_path = f.name

        try:
            cfg = load_config(config_path)
            assert cfg.enabled_events == event_list
        finally:
            os.unlink(config_path)

    @given(event_list=st.lists(invalid_event_type, min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_all_invalid_events_excluded(self, event_list):
        """
        When all items in enabled_events are invalid, the result is empty.
        """
        config_data = {
            "telegram_bot_token": "token",
            "telegram_chat_id": "chat",
            "enabled_events": event_list,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(config_data, f, default_flow_style=False)
            config_path = f.name

        try:
            cfg = load_config(config_path)
            assert cfg.enabled_events == []
        finally:
            os.unlink(config_path)
