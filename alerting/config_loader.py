# alerting/config_loader.py
#
# Reads alerting/config.yaml, applies environment variable overrides for
# credentials, validates all fields, and returns a typed AlertingConfig
# dataclass.  This function NEVER raises — it always returns a config
# (possibly with alerting disabled).
#
# Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7

import logging
import os
from dataclasses import dataclass
from typing import List

import yaml  # PyYAML — install with: pip install pyyaml

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
# Use the standard logging module directly rather than data_ingestion.logger,
# because the alerting module is independent of data_ingestion (separate
# concern in the project pipeline).  The orchestrator's structured JSON
# formatter will still capture these via the logging hierarchy.
logger = logging.getLogger("alerting")


# ---------------------------------------------------------------------------
# VALID_EVENT_TYPES
# ---------------------------------------------------------------------------
# The six recognized alert event types.  A frozenset is an immutable set —
# like a C++ const std::unordered_set<std::string> that cannot be modified
# after construction.  Lookups are O(1).
VALID_EVENT_TYPES = frozenset([
    "order_submitted",
    "order_filled",
    "skip_and_retry_error",
    "halt_the_loop_error",
    "kill_switch_triggered",
    "graceful_shutdown_completed",
])


# ---------------------------------------------------------------------------
# AlertingConfig dataclass
# ---------------------------------------------------------------------------
# @dataclass auto-generates __init__, __repr__, __eq__ — same as the project's
# other config dataclasses (data_ingestion.Config, OrchestratorConfig, etc.).
@dataclass
class AlertingConfig:
    """
    Validated configuration for the alerting module.

    Fields:
        telegram_bot_token   — Bot API token (empty string = disabled).
        telegram_chat_id     — Target chat ID (empty string = disabled).
        enabled_events       — Subset of VALID_EVENT_TYPES that trigger alerts.
        telegram_bot_enabled — Stretch goal: enable the Telegram bot listener.
        alerting_enabled     — Derived: True only if BOTH token AND chat_id
                               are non-empty strings.
    """
    telegram_bot_token: str
    telegram_chat_id: str
    enabled_events: List[str]
    telegram_bot_enabled: bool
    alerting_enabled: bool


# ---------------------------------------------------------------------------
# Helper: create a disabled config (used for multiple early-return paths)
# ---------------------------------------------------------------------------

def _disabled_config() -> AlertingConfig:
    """
    Return an AlertingConfig with alerting disabled and all defaults.

    This is a convenience factory for the many code paths in load_config()
    that bail out early (missing file, bad YAML, etc.).
    """
    return AlertingConfig(
        telegram_bot_token="",
        telegram_chat_id="",
        # list() creates a fresh list from the frozenset — each caller gets
        # their own mutable copy (not that they should mutate it, but
        # defensive copying avoids surprises).
        enabled_events=list(VALID_EVENT_TYPES),
        telegram_bot_enabled=False,
        alerting_enabled=False,
    )


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(config_path: str = "alerting/config.yaml") -> AlertingConfig:
    """
    Read alerting/config.yaml, apply env var overrides, and return a
    validated AlertingConfig.

    This function NEVER raises — it always returns a config (possibly
    disabled).  Any error encountered along the way is logged at WARNING
    or INFO level and results in alerting being disabled.

    Algorithm (9 steps from the design document):
      Step 1 — Check file existence. If absent → return disabled config.
      Step 2 — Parse YAML. If malformed → log WARNING, return disabled.
      Step 3 — Check root is dict. If not → log WARNING, return disabled.
      Step 4 — Extract telegram_bot_token; override with env var.
      Step 5 — Extract telegram_chat_id; override with env var.
      Step 6 — Determine alerting_enabled; warn on partial credentials.
      Step 7 — Validate enabled_events list.
      Step 8 — Extract telegram_bot_enabled.
      Step 9 — Return AlertingConfig.

    Args:
        config_path: Path to alerting/config.yaml (default).

    Returns:
        AlertingConfig instance (alerting_enabled may be False).
    """

    # ------------------------------------------------------------------
    # Step 1: Check file existence
    # ------------------------------------------------------------------
    # os.path.exists() returns False if the file doesn't exist.
    # No exception is raised — we handle the "missing" case ourselves.
    if not os.path.exists(config_path):
        # Config file absent is NOT an error for alerting — it just means
        # alerting is disabled.  Log a single INFO and return.
        logger.info(
            "Alerting config file not found at '%s'. "
            "Alerting is disabled.",
            config_path,
        )
        return _disabled_config()

    # ------------------------------------------------------------------
    # Step 2: Parse YAML
    # ------------------------------------------------------------------
    # 'try / except' wraps the entire file-read-and-parse so that ANY
    # failure (I/O error, YAML syntax error) is caught and logged.
    try:
        # 'with open(...) as f:' is a CONTEXT MANAGER — guarantees the file
        # handle is closed when the block exits (like RAII in C++).
        with open(config_path, "r", encoding="utf-8") as f:
            # yaml.safe_load() parses YAML text into a Python object.
            # 'safe_load' forbids arbitrary Python object construction
            # (security best-practice — same pattern as data_ingestion).
            raw = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as exc:
        # yaml.YAMLError — YAML syntax error.
        # OSError — file permission denied, disk I/O error, etc.
        logger.warning(
            "Failed to read or parse alerting config '%s': %s. "
            "Alerting is disabled.",
            config_path,
            str(exc)[:200],
        )
        return _disabled_config()

    # yaml.safe_load() returns None for an empty file.  Treat it as an
    # empty dict so the subsequent steps can run uniformly.
    if raw is None:
        raw = {}

    # ------------------------------------------------------------------
    # Step 3: Check root is dict
    # ------------------------------------------------------------------
    # isinstance(raw, dict) is True only if the YAML root is a mapping.
    # A YAML file containing just a list, a scalar, or nothing would fail.
    if not isinstance(raw, dict):
        logger.warning(
            "Alerting config '%s' root is not a mapping (got %s). "
            "Alerting is disabled.",
            config_path,
            type(raw).__name__,
        )
        return _disabled_config()

    # ------------------------------------------------------------------
    # Step 4: Extract telegram_bot_token (with env var override)
    # ------------------------------------------------------------------
    # dict.get(key, default) returns the default if the key is absent —
    # like C++ std::map::find() returning end() when the key is missing,
    # except get() returns a value directly instead of an iterator.
    telegram_bot_token = raw.get("telegram_bot_token", "")

    # Ensure we always have a string, even if the YAML value is None or int.
    if not isinstance(telegram_bot_token, str):
        telegram_bot_token = ""

    # Environment variable override: TELEGRAM_BOT_TOKEN takes precedence
    # over the YAML value when it is set and non-empty.
    # os.environ.get(key, default) reads an environment variable safely.
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if env_token:
        telegram_bot_token = env_token

    # ------------------------------------------------------------------
    # Step 5: Extract telegram_chat_id (with env var override)
    # ------------------------------------------------------------------
    telegram_chat_id = raw.get("telegram_chat_id", "")

    if not isinstance(telegram_chat_id, str):
        # YAML might parse a numeric chat ID as int — convert to string.
        telegram_chat_id = str(telegram_chat_id) if telegram_chat_id is not None else ""

    env_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if env_chat_id:
        telegram_chat_id = env_chat_id

    # ------------------------------------------------------------------
    # Step 6: Determine alerting_enabled
    # ------------------------------------------------------------------
    # Alerting is enabled only when BOTH token and chat_id are non-empty.
    token_present = len(telegram_bot_token) > 0
    chat_id_present = len(telegram_chat_id) > 0

    alerting_enabled = token_present and chat_id_present

    if not alerting_enabled:
        if token_present and not chat_id_present:
            # Exactly one credential set — warn about the missing one.
            logger.warning(
                "Incomplete Telegram configuration: 'telegram_bot_token' is "
                "set but 'telegram_chat_id' is missing. Alerting is disabled."
            )
        elif chat_id_present and not token_present:
            logger.warning(
                "Incomplete Telegram configuration: 'telegram_chat_id' is "
                "set but 'telegram_bot_token' is missing. Alerting is disabled."
            )
        else:
            # Both are empty — normal "disabled" state.
            logger.info(
                "Telegram credentials not configured. Alerting is disabled."
            )

    # ------------------------------------------------------------------
    # Step 7: Validate enabled_events
    # ------------------------------------------------------------------
    raw_events = raw.get("enabled_events", None)

    if raw_events is None:
        # Key absent or explicitly set to null → default to all six events.
        enabled_events = list(VALID_EVENT_TYPES)
    elif not isinstance(raw_events, list):
        # Not a list → warn and default to all six.
        logger.warning(
            "'enabled_events' in alerting config is not a list (got %s). "
            "Defaulting to all event types.",
            type(raw_events).__name__,
        )
        enabled_events = list(VALID_EVENT_TYPES)
    else:
        # Filter: keep only recognized event type strings.
        enabled_events = []
        for item in raw_events:
            if not isinstance(item, str):
                logger.warning(
                    "Non-string value in 'enabled_events': %r. Ignoring.",
                    item,
                )
                continue
            if item in VALID_EVENT_TYPES:
                enabled_events.append(item)
            else:
                logger.warning(
                    "Unrecognized event type in 'enabled_events': '%s'. "
                    "Ignoring. Valid types are: %s",
                    item,
                    ", ".join(sorted(VALID_EVENT_TYPES)),
                )

    # ------------------------------------------------------------------
    # Step 8: Extract telegram_bot_enabled
    # ------------------------------------------------------------------
    telegram_bot_enabled_raw = raw.get("telegram_bot_enabled", False)

    # Only accept an actual boolean.  YAML parses "true"/"false" as bool,
    # but we reject strings or integers that happen to be truthy.
    if isinstance(telegram_bot_enabled_raw, bool):
        telegram_bot_enabled = telegram_bot_enabled_raw
    else:
        telegram_bot_enabled = False

    # ------------------------------------------------------------------
    # Step 9: Return the validated AlertingConfig
    # ------------------------------------------------------------------
    return AlertingConfig(
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        enabled_events=enabled_events,
        telegram_bot_enabled=telegram_bot_enabled,
        alerting_enabled=alerting_enabled,
    )
