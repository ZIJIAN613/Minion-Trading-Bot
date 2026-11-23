# Design Document: Kill Switch & Alerting

## Overview

This feature adds two cross-cutting capabilities to the Minions paper-trading bot:

1. **File-based Kill Switch** — A mechanism to halt the Orchestrator loop immediately from outside the running process. The presence of a file at a configured path signals the loop to stop at the top of its next iteration, before any trading logic executes. This is distinct from the existing SIGINT/SIGTERM graceful shutdown.

2. **Telegram Alerting Module** — A new `alerting/` module that sends fire-and-forget notifications on key trading events (order fills, errors, kill switch activation, etc.) via the Telegram Bot API. Designed to never crash or block the main loop under any circumstances.

3. **Telegram Bot Remote Control (Stretch Goal)** — A daemon thread that listens for `/kill` and `/status` commands from Telegram, enabling remote kill switch activation and status reporting.

### Design Goals

- **Safety**: The kill switch provides an immediate external halt independent of the process's signal handlers. Alerting failures never affect trading operations.
- **Isolation**: The alerting module is fully decoupled from the trading pipeline — it catches all exceptions internally and enforces strict timeouts.
- **Explicitness**: Well-commented code following existing project patterns (YAML config, env var overrides, structured JSON logging).
- **Minimal Disruption**: Integration into the existing Orchestrator loop is surgical — a few lines inserted at well-defined points.

### Key Design Decisions

1. **File existence check (not content)**: The kill switch uses `os.path.exists()` — the simplest, most reliable cross-platform mechanism. No file watching, no inotify, no polling threads. The check runs at the top of every loop iteration and during interruptible sleeps.
2. **Synchronous alerting**: `send_alert()` blocks for up to 5 seconds per HTTP call. This is acceptable for a bot with multi-minute cycle intervals. No background threads or async complexity needed for alerting delivery.
3. **Fresh HTTP connection per call**: No persistent sessions or connection pooling. Each `send_alert()` call creates a new connection and releases it before returning. This prevents resource leaks and stale connections.
4. **Config-file-absent = alerting disabled**: If `alerting/config.yaml` doesn't exist, alerting is silently disabled. This means the bot works out-of-the-box without Telegram configuration.
5. **Kill switch check during sleep**: The existing `threading.Event.wait(timeout)` is split into shorter intervals with kill switch checks between them, providing sub-interval response latency without adding threads.
6. **Distinct exit codes**: Exit code 0 = graceful shutdown, 1 = kill switch activated (clean), 2 = kill switch shutdown sequence failure, 3 = post-order halt (clean), 4 = post-order halt shutdown sequence failure.

### Research Findings

- **Telegram Bot API `sendMessage`**: HTTP POST to `https://api.telegram.org/bot<token>/sendMessage` with JSON body `{"chat_id": "<id>", "text": "<msg>"}`. Max message length is 4096 characters. Returns HTTP 200 with `{"ok": true}` on success. No authentication beyond the bot token in the URL.
- **Python `urllib.request`**: Standard library HTTP client — no external dependency needed (unlike `requests`). Supports timeout parameter covering DNS + connect + read. Suitable for simple POST requests.
- **`os.path.exists()` behavior**: Returns False if the path doesn't exist OR if the process lacks permission to stat the parent directory. On permission errors, it may raise `OSError` on some platforms — hence the try/except around the kill switch check.

---

## Architecture

```mermaid
flowchart TD
    subgraph orchestrator
        CL[config_loader.py<br/>+kill_switch_file_path] --> LP[loop.py<br/>run_loop]
        SM[state_manager.py] --> LP
        SC[safety_check.py] --> LP
    end

    subgraph alerting["alerting/ (NEW)"]
        ACL[config_loader.py] --> SA[sender.py<br/>send_alert]
        ACL --> TB[telegram_bot.py<br/>(stretch)]
    end

    subgraph external
        TG[Telegram Bot API<br/>api.telegram.org]
    end

    LP -->|"1. kill switch check"| KSF[orchestrator/KILL_SWITCH<br/>(file on disk)]
    LP -->|"2. send_alert(event, ctx)"| SA
    SA -->|"HTTP POST /sendMessage"| TG
    TB -.->|"HTTP GET /getUpdates"| TG
    TB -.->|"create file"| KSF

    style KSF fill:#ffcdd2
    style TG fill:#ffe0b2
    style alerting fill:#e8f5e9
```

### Module Boundary

| Boundary | Direction | Data Shape |
|----------|-----------|-----------|
| `orchestrator` → Kill_Switch_File | Check | `os.path.exists(path)` → `bool` |
| `orchestrator` → `alerting` | Call | `send_alert(event_type: str, context: dict) → None` |
| `alerting` → Telegram API | HTTP POST | `{"chat_id": str, "text": str}` |
| `alerting/config.yaml` → `alerting` | Config | YAML with `telegram_bot_token`, `telegram_chat_id`, `enabled_events` |
| `orchestrator/config.yaml` → `orchestrator` | Config | YAML with `kill_switch_file_path` (new optional key) |
| Env vars → `alerting` | Override | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

### Separation of Concerns

- **`alerting/config_loader.py`** — Reads and validates `alerting/config.yaml`, applies env var overrides. Only YAML I/O in the alerting module.
- **`alerting/sender.py`** — The `send_alert()` function: formats messages, truncates, sends HTTP POST, catches all exceptions. Zero dependencies on orchestrator internals.
- **`alerting/telegram_bot.py`** — (Stretch goal) Daemon thread for `/kill` and `/status` commands.
- **`alerting/__init__.py`** — Public API surface: exports `send_alert` and `initialize`.
- **`orchestrator/config_loader.py`** — Extended with `kill_switch_file_path` field.
- **`orchestrator/loop.py`** — Extended with kill switch check at top of loop and `send_alert` calls at integration points.

---

## Components and Interfaces

### 1. `alerting/config_loader.py`

```python
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

logger = logging.getLogger("alerting")

# The six recognized alert event types.
VALID_EVENT_TYPES = frozenset([
    "order_submitted",
    "order_filled",
    "skip_and_retry_error",
    "halt_the_loop_error",
    "kill_switch_triggered",
    "graceful_shutdown_completed",
])


@dataclass
class AlertingConfig:
    """Validated configuration for the alerting module."""
    telegram_bot_token: str          # Bot API token (empty = disabled)
    telegram_chat_id: str            # Target chat ID (empty = disabled)
    enabled_events: List[str]        # Subset of VALID_EVENT_TYPES
    telegram_bot_enabled: bool       # Stretch: enable Telegram bot listener
    alerting_enabled: bool           # Derived: True if token AND chat_id are non-empty


def load_config(config_path: str = "alerting/config.yaml") -> AlertingConfig:
    """
    Read alerting/config.yaml, apply env var overrides, and return
    a validated AlertingConfig.

    Algorithm:
      Step 1 — Check file existence. If absent → return disabled config.
      Step 2 — Parse YAML. If malformed → log WARNING, return disabled config.
      Step 3 — Check root is dict. If not → log WARNING, return disabled config.
      Step 4 — Extract telegram_bot_token (str, default "").
               Override with TELEGRAM_BOT_TOKEN env var if set.
      Step 5 — Extract telegram_chat_id (str, default "").
               Override with TELEGRAM_CHAT_ID env var if set.
      Step 6 — Determine alerting_enabled = both token and chat_id are non-empty.
               If exactly one is set, log WARNING identifying which is missing.
      Step 7 — Validate enabled_events (list of strings, default all 6).
               Log WARNING for unrecognized values; keep only valid ones.
      Step 8 — Extract telegram_bot_enabled (bool, default False).
      Step 9 — Return AlertingConfig.

    This function NEVER raises — it always returns a config (possibly disabled).

    Args:
        config_path: Path to alerting/config.yaml.

    Returns:
        AlertingConfig instance.
    """
```

### 2. `alerting/sender.py`

```python
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("alerting")

# Timeout for each Telegram API call (seconds).
# The entire HTTP operation (DNS + connect + read) must complete within this.
_HTTP_TIMEOUT_SECONDS = 5

# Maximum Telegram message length (characters).
_MAX_MESSAGE_LENGTH = 4096

# Human-readable labels for each event type.
_EVENT_LABELS = {
    "order_submitted": "📤 Order Submitted",
    "order_filled": "✅ Order Filled",
    "skip_and_retry_error": "⚠️ Cycle Skipped (Retry)",
    "halt_the_loop_error": "🛑 Loop Halted (Error)",
    "kill_switch_triggered": "🔴 Kill Switch Triggered",
    "graceful_shutdown_completed": "🟢 Graceful Shutdown",
}

# Required context fields per event type.
_REQUIRED_CONTEXT = {
    "order_submitted": ["symbol", "order_id"],
    "order_filled": ["symbol", "order_id", "filled_quantity", "fill_price"],
    "skip_and_retry_error": ["error_summary", "cycle_number"],
    "halt_the_loop_error": ["error_summary", "symbol", "order_id"],
    "kill_switch_triggered": ["cycle_number"],
    "graceful_shutdown_completed": ["cycle_number"],
}


def _format_message(event_type: str, context: dict) -> str:
    """
    Format a Telegram alert message for the given event.

    Algorithm:
      1. Look up the human-readable label for event_type.
      2. Generate UTC timestamp in ISO 8601 format.
      3. Build context lines: for each required field of this event_type,
         use context.get(field, "N/A") to substitute missing values.
      4. Assemble the final message: label + timestamp + context lines.
      5. Truncate to _MAX_MESSAGE_LENGTH characters.

    Args:
        event_type: One of the six valid Alert_Event strings.
        context:    Dict of context values (may be incomplete).

    Returns:
        Formatted message string, guaranteed ≤ 4096 characters.
    """


def send_alert(
    event_type: str,
    context: dict,
    *,
    config: "AlertingConfig" = None,
) -> None:
    """
    Dispatch a Telegram alert for the given event. Fire-and-forget.

    This function NEVER raises — all exceptions are caught internally.

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
        config:     AlertingConfig instance (injected at module init).

    Returns:
        None (always).
    """
```

### 3. `alerting/__init__.py`

```python
"""
alerting/ — Telegram notification module for the Minions paper-trading bot.

Exposes a single fire-and-forget function: send_alert(event_type, context).
This function NEVER raises exceptions and NEVER blocks for more than 10 seconds.

Usage:
    from alerting import initialize, send_alert

    initialize()  # Load config, log status
    send_alert("order_filled", {"symbol": "AAPL", "fill_price": 150.25, ...})
"""

from alerting.config_loader import AlertingConfig, load_config, VALID_EVENT_TYPES
from alerting.sender import send_alert as _send_alert

# Module-level config — set by initialize().
_config: AlertingConfig = None


def initialize(config_path: str = "alerting/config.yaml") -> None:
    """
    Load alerting configuration and log status.

    Must be called once during Orchestrator initialization.
    If this raises (which it shouldn't — load_config catches errors),
    the caller logs WARNING and continues with alerting disabled.

    Args:
        config_path: Path to alerting/config.yaml.
    """
    global _config
    _config = load_config(config_path)


def send_alert(event_type: str, context: dict) -> None:
    """
    Public API: dispatch a Telegram alert. Fire-and-forget.

    Delegates to sender.send_alert with the module-level config.
    If initialize() was never called or config is None, returns silently.
    """
    _send_alert(event_type, context, config=_config)


__all__ = ["initialize", "send_alert", "VALID_EVENT_TYPES"]
```

### 4. `alerting/telegram_bot.py` (Stretch Goal)

```python
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("alerting")

_MAX_CONSECUTIVE_FAILURES = 5
_POLL_TIMEOUT_SECONDS = 30  # Long-polling timeout for getUpdates


class TelegramBotListener:
    """
    Daemon thread that listens for /kill and /status commands via Telegram.

    Runs in a separate thread from the main Orchestrator loop.
    Only accepts commands from the configured telegram_chat_id.
    Terminates itself after 5 consecutive polling failures.

    This class is only instantiated when telegram_bot_enabled=True
    AND both credentials are configured.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        kill_switch_file_path: str,
        get_status_callback,  # callable() -> dict
    ):
        """
        Args:
            bot_token:             Telegram bot API token.
            chat_id:              Authorized chat ID (commands from others are ignored).
            kill_switch_file_path: Path where to create the kill switch file.
            get_status_callback:   Callable returning current loop status dict.
        """

    def start(self) -> None:
        """Start the listener daemon thread."""

    def stop(self) -> None:
        """Signal the listener to stop (called during shutdown)."""
```

### 5. Extended `orchestrator/config_loader.py` (Changes)

The existing `OrchestratorConfig` dataclass gains one new optional field:

```python
@dataclass
class OrchestratorConfig:
    bar_check_interval_seconds: float  # [60, 3600]
    symbol: str                         # 1–10 chars, default "AAPL"
    state_file_path: str                # 1–255 chars, default "orchestrator/state.json"
    kill_switch_file_path: str          # 1–255 chars, default "orchestrator/KILL_SWITCH"
```

Validation in `load_config()` adds Step 5b for the new field:
- Type: string (reject non-string, booleans)
- Length: 1–255 characters (reject empty or >255)
- Default: `"orchestrator/KILL_SWITCH"`

### 6. Extended `orchestrator/loop.py` (Integration Points)

```python
# --- Kill Switch Check (top of main loop, BEFORE stop_flag check) ---

def _check_kill_switch(file_path: str) -> bool:
    """
    Check whether the kill switch file exists.

    Returns True if the file exists (kill switch is active).
    Returns False if the file does not exist.
    If os.path.exists() raises an OS exception, logs WARNING and returns False
    (failed check does NOT halt the loop).

    Args:
        file_path: Configured kill switch file path.

    Returns:
        True if kill switch should activate, False otherwise.
    """


# --- Interruptible sleep with kill switch check ---

def _interruptible_wait(
    shutdown_event: threading.Event,
    timeout: float,
    kill_switch_file_path: str,
    check_interval: float = 5.0,
) -> str:
    """
    Sleep for up to `timeout` seconds, but wake early if:
    - shutdown_event is set (graceful shutdown), OR
    - the kill switch file appears on disk.

    Splits the wait into `check_interval`-second chunks. Between each chunk,
    checks both the shutdown_event and the kill switch file.

    Args:
        shutdown_event:         threading.Event for graceful shutdown.
        timeout:                Total sleep time in seconds.
        kill_switch_file_path:  Path to check for kill switch file.
        check_interval:         How often to wake and check (default 5s).

    Returns:
        "timeout"    — full wait elapsed without interruption.
        "shutdown"   — shutdown_event was set.
        "kill_switch" — kill switch file detected during wait.
    """
```

**Integration into `run_loop()` main loop:**

```python
while True:
    # NEW: Kill switch check — FIRST operation, before stop_flag.
    if _check_kill_switch(orch_config.kill_switch_file_path):
        _kill_switch_shutdown(orch_config, state_data, cycle_number)

    # EXISTING: Check stop_flag → graceful shutdown.
    if stop_flag:
        # ... existing shutdown logic ...
        send_alert("graceful_shutdown_completed", {"cycle_number": cycle_number})
        sys.exit(0)

    # ... rest of loop (new-bar check, cycle execution, etc.) ...

    # MODIFIED: Replace shutdown_event.wait() with _interruptible_wait()
    wake_reason = _interruptible_wait(
        shutdown_event, orch_config.bar_check_interval_seconds,
        orch_config.kill_switch_file_path,
    )
    # If wake_reason == "kill_switch", the top-of-loop check will catch it.
```

**Shutdown Sequence Helpers (exit code 2/4 paths):**

Each shutdown sequence wraps all steps in a single `try/except` so that a failure in *any* step (log, alert, or state flush) routes to the "dirty" exit code rather than producing an unhandled traceback. `sys.exit()` raises `SystemExit` (a subclass of `BaseException`, NOT `Exception`), so it passes through the `except Exception` cleanly.

```python
def _kill_switch_shutdown(orch_config, state_data, cycle_number):
    """
    Kill switch shutdown sequence.
    Exit code 1 = all steps succeeded (clean).
    Exit code 2 = one or more steps failed (dirty).
    """
    try:
        # Step (a): Log CRITICAL structured event.
        logger.critical(
            "Kill switch triggered.",
            extra={"event": "kill_switch_triggered",
                   "file_path": orch_config.kill_switch_file_path},
        )
        # Step (b): Dispatch Telegram alert.
        # send_alert() itself never raises, but we're inside the try
        # for defense-in-depth.
        send_alert("kill_switch_triggered", {"cycle_number": cycle_number})
        # Step (c): Flush state file to disk (CAN raise OSError).
        save_state(state_data, orch_config.state_file_path)
        # All steps succeeded → clean kill switch exit.
        sys.exit(1)
    except SystemExit:
        # sys.exit(1) raises SystemExit — let it propagate normally.
        raise
    except Exception as e:
        # Any step failed (most likely save_state OSError) → dirty exit.
        logger.warning(
            "Kill switch shutdown sequence failed.",
            extra={
                "event": "kill_switch_shutdown_failed",
                "error_type": type(e).__name__,
                "error_message": str(e)[:200],
            },
        )
        sys.exit(2)


def _post_order_halt_shutdown(orch_config, state_data, cycle_number, error_context):
    """
    Post-order failure shutdown sequence.
    Exit code 3 = all steps succeeded (clean).
    Exit code 4 = one or more steps failed (dirty).
    """
    try:
        # Step (a): Dispatch Telegram alert with error context.
        send_alert("halt_the_loop_error", {
            "error_summary": str(error_context.get("error_details", ""))[:200],
            "symbol": error_context.get("symbol", ""),
            "order_id": error_context.get("entry_order_id", ""),
        })
        # Step (b): Flush state file to disk.
        save_state(state_data, orch_config.state_file_path)
        # All steps succeeded → clean post-order halt.
        sys.exit(3)
    except SystemExit:
        raise
    except Exception as e:
        # Shutdown sequence itself failed → dirty exit.
        logger.warning(
            "Post-order halt shutdown sequence failed.",
            extra={
                "event": "post_order_halt_shutdown_failed",
                "error_type": type(e).__name__,
                "error_message": str(e)[:200],
            },
        )
        sys.exit(4)
```

---

## Data Models

### AlertingConfig

```python
@dataclass
class AlertingConfig:
    telegram_bot_token: str          # Empty string = disabled
    telegram_chat_id: str            # Empty string = disabled
    enabled_events: List[str]        # Subset of 6 valid event types
    telegram_bot_enabled: bool       # Stretch: bot listener enabled
    alerting_enabled: bool           # Derived: token AND chat_id non-empty
```

### Extended OrchestratorConfig

```python
@dataclass
class OrchestratorConfig:
    bar_check_interval_seconds: float  # [60, 3600]
    symbol: str                         # 1–10 chars, default "AAPL"
    state_file_path: str                # 1–255 chars, default "orchestrator/state.json"
    kill_switch_file_path: str          # 1–255 chars, default "orchestrator/KILL_SWITCH"
```

### Alert Message Format

Each Telegram message follows this structure:

```
📤 Order Submitted
Time: 2024-06-15T14:30:00Z

Symbol: AAPL
Order ID: abc-123-def
```

The label line uses emoji prefix for quick visual identification in Telegram.
Context fields are listed one per line with "Field: value" format.
Missing required fields are substituted with "N/A".
Total message is truncated to 4096 characters if needed.

### alerting/config.yaml Schema

```yaml
# alerting/config.yaml
# ====================
# Configuration for Telegram alerting.

# telegram_bot_token: Bot API token from @BotFather.
# Prefer using TELEGRAM_BOT_TOKEN env var instead of storing here.
# Type: string, default: "" (disabled). OPTIONAL.
telegram_bot_token: ""

# telegram_chat_id: Chat ID to send alerts to.
# Prefer using TELEGRAM_CHAT_ID env var.
# Type: string, default: "" (disabled). OPTIONAL.
telegram_chat_id: ""

# enabled_events: Which events trigger alerts.
# Type: list of strings, default: all six event types. OPTIONAL.
# enabled_events:
#   - order_submitted
#   - order_filled
#   - skip_and_retry_error
#   - halt_the_loop_error
#   - kill_switch_triggered
#   - graceful_shutdown_completed

# telegram_bot_enabled: Enable the remote control bot (stretch goal).
# Type: bool, default: false. OPTIONAL.
# Requires both telegram_bot_token and telegram_chat_id to be set.
telegram_bot_enabled: false
```

### Exit Code Summary

| Code | Meaning | Alert Dispatched |
|------|---------|-----------------|
| 0 | Graceful shutdown (SIGINT/SIGTERM) | `graceful_shutdown_completed` |
| 1 | Kill switch activated (clean) | `kill_switch_triggered` |
| 2 | Kill switch shutdown sequence failed | Attempted but may have failed |
| 3 | Post-order failure halt (clean) | `halt_the_loop_error` |
| 4 | Post-order halt shutdown sequence failed | Attempted but may have failed |

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Kill switch triggers regardless of file content

*For any* file content (empty bytes, random UTF-8 text, binary data of any length from 0 to 10,000 bytes), if a file exists at the configured `kill_switch_file_path`, then `_check_kill_switch()` SHALL return `True` — the decision is based solely on file existence, never on content.

**Validates: Requirements 1.4**

### Property 2: Alert message formatting includes required fields with N/A substitution

*For any* valid event_type (one of the six recognized types) and *for any* context dictionary (with an arbitrary subset of the required fields present, and the remaining fields absent), the formatted message SHALL contain: (a) the human-readable event label, (b) a valid UTC ISO 8601 timestamp, and (c) for each required context field, either the provided value or the literal string "N/A" if the field is missing from the context dict.

**Validates: Requirements 3.2**

### Property 3: Alert message truncation to Telegram maximum

*For any* valid event_type and *for any* context dictionary containing string values of arbitrary length (up to 100,000 characters per field), the final formatted message passed to the HTTP call SHALL be at most 4096 characters in length.

**Validates: Requirements 3.3**

### Property 4: send_alert never raises exceptions

*For any* input combination — valid or invalid `event_type` string, any `context` dict (including empty, None values, deeply nested, or non-serializable objects), and regardless of whether the HTTP call succeeds, times out, or raises any exception type (excluding `KeyboardInterrupt`) — the `send_alert()` function SHALL return `None` without propagating any exception to the caller.

**Validates: Requirements 3.5, 4.2**

### Property 5: Unrecognized event_type prevents message delivery

*For any* string that is not one of the six recognized Alert_Event types (`order_submitted`, `order_filled`, `skip_and_retry_error`, `halt_the_loop_error`, `kill_switch_triggered`, `graceful_shutdown_completed`), calling `send_alert()` with that string SHALL NOT result in any HTTP request to the Telegram API.

**Validates: Requirements 3.10**

### Property 6: enabled_events config validation retains only recognized values

*For any* list of strings provided as `enabled_events` in the config (containing a mix of valid event type names and arbitrary invalid strings), the resulting `AlertingConfig.enabled_events` SHALL contain exactly those strings from the input list that are members of the six recognized event types, preserving their order — all unrecognized strings SHALL be excluded.

**Validates: Requirements 5.4**

### Property 7: kill_switch_file_path config validation

*For any* value provided as `kill_switch_file_path` in the orchestrator config: if the value is a string with length in [1, 255], `load_config` SHALL accept it and include it in the returned `OrchestratorConfig`. If the value is not a string, is an empty string, or has length > 255, `load_config` SHALL raise `ValueError`.

**Validates: Requirements 7.1, 7.2**

---

## Error Handling

### Kill Switch Error Matrix

| Scenario | Behavior | Exit Code |
|----------|----------|-----------|
| Kill switch file exists, shutdown sequence succeeds | Log CRITICAL, alert, flush, halt | 1 |
| Kill switch file exists, shutdown sequence fails | Halt immediately | 2 |
| `os.path.exists()` raises OSError/PermissionError | Log WARNING, continue loop | — |
| Kill switch file path invalid at startup | Raise ValueError, prevent startup | — |

### Alerting Error Matrix

| Scenario | Behavior | Impact on Trading |
|----------|----------|-------------------|
| Telegram API timeout (>5s) | Log WARNING, return None | None |
| Telegram API HTTP error (4xx, 5xx) | Log WARNING, return None | None |
| Network unreachable | Log WARNING, return None | None |
| Invalid bot token / chat ID | Log WARNING, return None | None |
| Malformed config.yaml | Log WARNING, alerting disabled | None |
| Config file missing | Alerting disabled (INFO log) | None |
| Unexpected runtime error in send_alert | Catch all, log WARNING, return None | None |
| Message too long | Truncate to 4096 chars, send | None |
| Unrecognized event_type | Log WARNING, skip send | None |

### Orchestrator Integration Error Handling

| Integration Point | Failure Behavior |
|-------------------|-----------------|
| `alerting.initialize()` raises | Log WARNING, continue with alerting disabled |
| `send_alert()` during kill switch shutdown | If it fails, continue to flush + exit(1) |
| `send_alert()` during post-order halt | If it fails, continue to flush + exit(3) |
| `send_alert()` during graceful shutdown | If it fails, continue to exit(0) |
| `send_alert()` after order fill | If it fails, cycle continues normally |

### Design Principle: Alerting Never Blocks Trading

The entire alerting module is designed around one invariant: **no alerting failure can prevent, delay, or corrupt a trading operation**. This is enforced through:

1. **Exception swallowing**: `send_alert()` catches `Exception` (not `BaseException` — `KeyboardInterrupt` and `SystemExit` propagate normally).
2. **Hard timeout**: 5-second HTTP timeout ensures network issues don't stall the loop indefinitely.
3. **No shared state**: The alerting module holds no mutable state between calls — no connection pools, no retry queues, no background threads (for the non-stretch-goal version).
4. **Graceful degradation**: Missing config, invalid config, or runtime errors all degrade to "alerting disabled" rather than crashing.

---

## Testing Strategy

### Property-Based Tests (Hypothesis)

This feature is well-suited to property-based testing for its pure functions (message formatting, config validation, kill switch detection). The project already uses Hypothesis (visible from `.hypothesis/` directory).

**Configuration:**
- Library: [Hypothesis](https://hypothesis.readthedocs.io/) (already in project)
- Minimum iterations: 100 per property test
- Each test tagged with: `# Feature: kill-switch-alerting, Property {N}: {title}`

**Properties to implement:**
1. Kill switch file content independence (Property 1)
2. Message formatting correctness (Property 2)
3. Message truncation bound (Property 3)
4. send_alert exception safety (Property 4)
5. Invalid event_type rejection (Property 5)
6. enabled_events filtering (Property 6)
7. kill_switch_file_path validation (Property 7)

### Unit Tests (Example-Based)

- Kill switch shutdown sequence (exit codes 1, 2)
- Kill switch does not modify Stop_Flag
- Kill switch + Stop_Flag precedence (kill switch wins)
- Kill switch file not deleted after trigger
- Alerting disabled when credentials empty
- Alerting disabled when config file missing
- Partial credentials (one set, one missing) → WARNING
- HTTP timeout parameter verification (5 seconds)
- Correct integration points (alert calls at right moments)
- Malformed YAML → graceful degradation
- Env var override precedence

### Integration Tests

- Full loop iteration with kill switch file present → exit code 1
- Kill switch detection during interruptible sleep
- End-to-end alert delivery with mocked Telegram API
- Orchestrator startup with alerting module failure → continues trading

### Stretch Goal Tests (Telegram Bot)

- `/kill` creates file, `/status` reports state
- Commands from unauthorized chat IDs are ignored
- 5 consecutive poll failures → thread self-terminates
- Bot thread doesn't prevent process exit (daemon thread)
