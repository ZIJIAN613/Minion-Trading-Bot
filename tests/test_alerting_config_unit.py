"""
tests/test_alerting_config_unit.py — Example-based unit tests for alerting config_loader.

These tests cover specific scenarios and edge cases from the design document's
"Unit Tests" section that are not well-suited to property-based testing:
  - Both credentials empty → alerting disabled
  - Config file missing → same as empty credentials
  - Partial credentials (one set, one missing) → WARNING log
  - Environment variable override precedence
  - Malformed YAML → WARNING, no exception
  - Numeric chat_id coercion to string

Validates: Requirements 3.8, 3.9, 5.2, 5.3, 5.5, 5.6, 5.7

Run with:  python -m pytest tests/test_alerting_config_unit.py -v
"""

import logging
import os

import pytest
import yaml

from alerting.config_loader import (
    AlertingConfig,
    load_config,
    VALID_EVENT_TYPES,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def write_config(tmp_path):
    """
    Factory fixture that writes a YAML config dict to a temp file and
    returns the file path.  Avoids repeating the same 3 lines in every test.

    Usage:
        config_path = write_config({"telegram_bot_token": "tok", ...})
    """
    def _write(data: dict) -> str:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(data, default_flow_style=False),
            encoding="utf-8",
        )
        return str(config_file)
    return _write


@pytest.fixture
def write_raw_config(tmp_path):
    """
    Factory fixture that writes raw text (not YAML-serialized) to a temp
    config file.  Used for malformed YAML tests.
    """
    def _write(text: str) -> str:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(text, encoding="utf-8")
        return str(config_file)
    return _write


# ===========================================================================
# Test: Both credentials empty → alerting disabled
# ===========================================================================


class TestBothCredentialsEmpty:
    """When both token and chat_id are empty, alerting is disabled."""

    def test_alerting_disabled_with_empty_credentials(self, write_config, caplog):
        """
        If both telegram_bot_token and telegram_chat_id are empty strings,
        alerting_enabled SHALL be False and a single INFO log SHALL be
        emitted at initialization.
        """
        config_path = write_config({
            "telegram_bot_token": "",
            "telegram_chat_id": "",
        })

        # caplog captures log records — pytest's built-in fixture for
        # asserting on log output.  Like a C++ test spy on the logger.
        with caplog.at_level(logging.DEBUG, logger="alerting"):
            cfg = load_config(config_path)

        assert cfg.alerting_enabled is False
        assert cfg.telegram_bot_token == ""
        assert cfg.telegram_chat_id == ""

        # Check INFO log about alerting being disabled.
        info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_messages) >= 1
        assert "disabled" in info_messages[0].getMessage().lower()

    def test_no_warning_when_both_empty(self, write_config, caplog):
        """
        When BOTH credentials are absent, no WARNING should fire — only
        INFO (Requirement 3.8: single INFO at init is sufficient).
        """
        config_path = write_config({})

        with caplog.at_level(logging.DEBUG, logger="alerting"):
            cfg = load_config(config_path)

        assert cfg.alerting_enabled is False
        warning_messages = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_messages) == 0


# ===========================================================================
# Test: Config file missing → alerting disabled, no error
# ===========================================================================


class TestConfigFileMissing:
    """When config.yaml doesn't exist, alerting is disabled silently."""

    def test_missing_config_returns_disabled(self, tmp_path, caplog):
        """
        If alerting/config.yaml does not exist, load_config SHALL return a
        disabled config without raising any exception.
        """
        missing_path = str(tmp_path / "nonexistent" / "config.yaml")

        with caplog.at_level(logging.DEBUG, logger="alerting"):
            cfg = load_config(missing_path)

        assert cfg.alerting_enabled is False
        assert cfg.telegram_bot_token == ""
        assert cfg.telegram_chat_id == ""
        # Should have all six default events even when disabled.
        assert set(cfg.enabled_events) == VALID_EVENT_TYPES

    def test_missing_config_logs_info_not_warning(self, tmp_path, caplog):
        """
        Missing config file triggers INFO log, NOT a WARNING — this is a
        normal "alerting not configured" state, not an error.
        """
        missing_path = str(tmp_path / "does_not_exist.yaml")

        with caplog.at_level(logging.DEBUG, logger="alerting"):
            load_config(missing_path)

        warning_messages = [r for r in caplog.records if r.levelno == logging.WARNING]
        info_messages = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(warning_messages) == 0
        assert len(info_messages) >= 1


# ===========================================================================
# Test: Partial credentials → WARNING identifying the missing one
# ===========================================================================


class TestPartialCredentials:
    """When exactly one credential is set, a WARNING identifies the missing one."""

    def test_token_set_chat_id_missing(self, write_config, caplog):
        """
        If telegram_bot_token is set but telegram_chat_id is empty,
        load_config SHALL log a WARNING mentioning 'telegram_chat_id'.
        """
        config_path = write_config({
            "telegram_bot_token": "my-bot-token-123",
            "telegram_chat_id": "",
        })

        with caplog.at_level(logging.DEBUG, logger="alerting"):
            cfg = load_config(config_path)

        assert cfg.alerting_enabled is False
        assert cfg.telegram_bot_token == "my-bot-token-123"

        # Find the WARNING about the missing credential.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        # The message should identify which credential is missing.
        assert "chat_id" in warnings[0].getMessage().lower()

    def test_chat_id_set_token_missing(self, write_config, caplog):
        """
        If telegram_chat_id is set but telegram_bot_token is empty,
        load_config SHALL log a WARNING mentioning 'telegram_bot_token'.
        """
        config_path = write_config({
            "telegram_bot_token": "",
            "telegram_chat_id": "123456789",
        })

        with caplog.at_level(logging.DEBUG, logger="alerting"):
            cfg = load_config(config_path)

        assert cfg.alerting_enabled is False
        assert cfg.telegram_chat_id == "123456789"

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "token" in warnings[0].getMessage().lower()


# ===========================================================================
# Test: Environment variable override precedence
# ===========================================================================


class TestEnvVarOverride:
    """Env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID override YAML values."""

    def test_env_var_overrides_yaml_token(self, write_config, monkeypatch):
        """
        If both YAML and env var provide telegram_bot_token, the env var
        SHALL take precedence.
        """
        config_path = write_config({
            "telegram_bot_token": "yaml-token",
            "telegram_chat_id": "chat-123",
        })

        # monkeypatch.setenv() sets an environment variable for the duration
        # of this test — automatically cleaned up after the test (like RAII).
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")

        cfg = load_config(config_path)

        assert cfg.telegram_bot_token == "env-token"
        assert cfg.alerting_enabled is True

    def test_env_var_overrides_yaml_chat_id(self, write_config, monkeypatch):
        """
        If both YAML and env var provide telegram_chat_id, the env var
        SHALL take precedence.
        """
        config_path = write_config({
            "telegram_bot_token": "token-abc",
            "telegram_chat_id": "yaml-chat",
        })

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat-999")

        cfg = load_config(config_path)

        assert cfg.telegram_chat_id == "env-chat-999"
        assert cfg.alerting_enabled is True

    def test_env_var_enables_alerting_when_yaml_empty(self, write_config, monkeypatch):
        """
        If YAML has empty credentials but env vars provide both, alerting
        SHALL be enabled.
        """
        config_path = write_config({
            "telegram_bot_token": "",
            "telegram_chat_id": "",
        })

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")

        cfg = load_config(config_path)

        assert cfg.alerting_enabled is True
        assert cfg.telegram_bot_token == "env-token"
        assert cfg.telegram_chat_id == "env-chat"

    def test_empty_env_var_does_not_override(self, write_config, monkeypatch):
        """
        An empty env var (set but empty string) SHALL NOT override the
        YAML value — only a non-empty env var takes precedence.
        """
        config_path = write_config({
            "telegram_bot_token": "yaml-token",
            "telegram_chat_id": "yaml-chat",
        })

        # Set env var to empty string — should NOT override.
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

        cfg = load_config(config_path)

        # YAML value should be preserved because env var is empty.
        assert cfg.telegram_bot_token == "yaml-token"


# ===========================================================================
# Test: Malformed YAML → WARNING, no exception
# ===========================================================================


class TestMalformedYaml:
    """Malformed YAML logs a WARNING and returns a disabled config."""

    def test_syntax_error_returns_disabled_config(self, write_raw_config, caplog):
        """
        If config.yaml contains invalid YAML syntax, load_config SHALL
        log a WARNING and return a disabled config — no exception raised.
        """
        # Write deliberately malformed YAML (unmatched bracket + bad indent).
        config_path = write_raw_config("{\n  invalid: [unclosed\n  broken")

        with caplog.at_level(logging.DEBUG, logger="alerting"):
            cfg = load_config(config_path)

        # Must not raise — returns disabled config.
        assert cfg.alerting_enabled is False

        # A WARNING should have been logged about the parse error.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "parse" in warnings[0].getMessage().lower() or \
               "read" in warnings[0].getMessage().lower() or \
               "failed" in warnings[0].getMessage().lower()

    def test_non_mapping_root_returns_disabled_config(self, write_raw_config, caplog):
        """
        If the YAML root is a list (not a mapping), load_config SHALL
        log a WARNING and return a disabled config.
        """
        # YAML that parses successfully but to a list, not a dict.
        config_path = write_raw_config("- item1\n- item2\n- item3\n")

        with caplog.at_level(logging.DEBUG, logger="alerting"):
            cfg = load_config(config_path)

        assert cfg.alerting_enabled is False

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "list" in warnings[0].getMessage().lower() or \
               "mapping" in warnings[0].getMessage().lower()


# ===========================================================================
# Test: Numeric chat_id in YAML coerced to string
# ===========================================================================


class TestNumericChatIdCoercion:
    """YAML numeric telegram_chat_id is coerced to a string."""

    def test_integer_chat_id_becomes_string(self, write_config):
        """
        If telegram_chat_id is a YAML integer (e.g., 123456789), load_config
        SHALL coerce it to the string "123456789" so it can be used in the
        Telegram API URL.
        """
        config_path = write_config({
            "telegram_bot_token": "my-token",
            "telegram_chat_id": 123456789,  # YAML will parse this as int
        })

        cfg = load_config(config_path)

        assert cfg.telegram_chat_id == "123456789"
        assert isinstance(cfg.telegram_chat_id, str)
        assert cfg.alerting_enabled is True

    def test_negative_integer_chat_id(self, write_config):
        """
        Negative chat IDs are valid in Telegram (group chats use negative
        IDs like -1001234567890).  They should be coerced to string.
        """
        config_path = write_config({
            "telegram_bot_token": "my-token",
            "telegram_chat_id": -1001234567890,
        })

        cfg = load_config(config_path)

        assert cfg.telegram_chat_id == "-1001234567890"
        assert isinstance(cfg.telegram_chat_id, str)
        assert cfg.alerting_enabled is True
