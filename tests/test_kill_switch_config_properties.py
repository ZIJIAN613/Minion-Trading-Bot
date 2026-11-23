"""
tests/test_kill_switch_config_properties.py — Property-based test for
kill_switch_file_path config validation.

# Feature: kill-switch-alerting, Property 7: kill_switch_file_path config
# validation

Uses Hypothesis to verify that the orchestrator's load_config() correctly
validates the kill_switch_file_path field:
  - Accepts strings with length in [1, 255]
  - Rejects non-string values (int, float, None, list, dict, bool)
  - Rejects empty string
  - Rejects strings longer than 255 characters

Validates: Requirements 7.1, 7.2

Run with:  python -m pytest tests/test_kill_switch_config_properties.py -v
"""

import os
import tempfile

import pytest
import yaml

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from orchestrator.config_loader import OrchestratorConfig, load_config


# ===========================================================================
# Helper: write a config dict to a temp YAML file
# ===========================================================================

def _write_config(config_data: dict) -> str:
    """
    Write a config dict to a temporary YAML file and return the file path.

    Uses tempfile (not tmp_path fixture) because Hypothesis does not reset
    pytest function-scoped fixtures between generated inputs — same pattern
    used in test_orchestrator_properties.py.
    """
    tmp_dir = tempfile.mkdtemp()
    config_file_path = os.path.join(tmp_dir, "config.yaml")
    with open(config_file_path, "w", encoding="utf-8") as f:
        yaml.dump(config_data, f)
    return config_file_path


# ===========================================================================
# Hypothesis strategies
# ===========================================================================

# Strategy: a valid kill_switch_file_path (non-empty string, 1–255 chars).
# Uses characters that are plausible in file paths.
valid_kill_switch_path = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd"),
        whitelist_characters="/\\._-"
    ),
    min_size=1,
    max_size=255,
)

# Strategy: a string that is too long (>255 chars).
too_long_path = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
    min_size=256,
    max_size=500,
)

# Strategy: a non-string value (int, float, None, list, dict, bool).
non_string_value = st.one_of(
    st.integers(min_value=-1000, max_value=1000),
    st.floats(min_value=-100, max_value=100, allow_nan=False, allow_infinity=False),
    st.just(None),
    st.just([]),
    st.just(["path"]),
    st.just({}),
    st.just({"key": "value"}),
    st.booleans(),
)


# ===========================================================================
# Property 7: kill_switch_file_path config validation
# ===========================================================================
# Feature: kill-switch-alerting, Property 7: kill_switch_file_path config
# validation


class TestKillSwitchFilePathValidation:
    """
    Property 7: For any value provided as kill_switch_file_path in the
    orchestrator config: if the value is a string with length in [1, 255],
    load_config SHALL accept it and include it in the returned
    OrchestratorConfig. If the value is not a string, is an empty string,
    or has length > 255, load_config SHALL raise ValueError.
    """

    @given(path=valid_kill_switch_path)
    @settings(max_examples=200)
    def test_valid_paths_accepted(self, path):
        """
        Strings of length 1–255 are accepted and preserved in the config.
        """
        config_data = {
            "bar_check_interval_seconds": 300,
            "kill_switch_file_path": path,
        }
        config_file = _write_config(config_data)

        result = load_config(config_file)

        assert isinstance(result, OrchestratorConfig)
        assert result.kill_switch_file_path == path

    @given(value=non_string_value)
    @settings(max_examples=100)
    def test_non_string_values_rejected(self, value):
        """
        Non-string values (int, float, None, list, dict, bool) raise ValueError.
        """
        config_data = {
            "bar_check_interval_seconds": 300,
            "kill_switch_file_path": value,
        }
        config_file = _write_config(config_data)

        with pytest.raises(ValueError) as exc_info:
            load_config(config_file)

        # The error message should identify the problematic key.
        assert "kill_switch_file_path" in str(exc_info.value)

    @given(path=too_long_path)
    @settings(max_examples=100)
    def test_too_long_paths_rejected(self, path):
        """
        Strings longer than 255 characters raise ValueError.
        """
        # Ensure the generated string is actually >255 chars.
        assume(len(path) > 255)

        config_data = {
            "bar_check_interval_seconds": 300,
            "kill_switch_file_path": path,
        }
        config_file = _write_config(config_data)

        with pytest.raises(ValueError) as exc_info:
            load_config(config_file)

        assert "kill_switch_file_path" in str(exc_info.value)

    def test_empty_string_rejected(self):
        """
        An empty string raises ValueError (length < 1).
        """
        config_data = {
            "bar_check_interval_seconds": 300,
            "kill_switch_file_path": "",
        }
        config_file = _write_config(config_data)

        with pytest.raises(ValueError) as exc_info:
            load_config(config_file)

        assert "kill_switch_file_path" in str(exc_info.value)

    def test_absent_key_uses_default(self):
        """
        When kill_switch_file_path is not in the config, the default
        "orchestrator/KILL_SWITCH" is applied.
        """
        config_data = {
            "bar_check_interval_seconds": 300,
        }
        config_file = _write_config(config_data)

        result = load_config(config_file)

        assert result.kill_switch_file_path == "orchestrator/KILL_SWITCH"

    def test_exactly_255_chars_accepted(self):
        """
        A string of exactly 255 characters is at the boundary and accepted.
        """
        path = "a" * 255
        config_data = {
            "bar_check_interval_seconds": 300,
            "kill_switch_file_path": path,
        }
        config_file = _write_config(config_data)

        result = load_config(config_file)
        assert result.kill_switch_file_path == path
        assert len(result.kill_switch_file_path) == 255

    def test_exactly_1_char_accepted(self):
        """
        A string of exactly 1 character is at the lower boundary and accepted.
        """
        config_data = {
            "bar_check_interval_seconds": 300,
            "kill_switch_file_path": "X",
        }
        config_file = _write_config(config_data)

        result = load_config(config_file)
        assert result.kill_switch_file_path == "X"
