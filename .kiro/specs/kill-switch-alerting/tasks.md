# Implementation Plan: Kill Switch & Alerting

## Overview

This plan implements the file-based kill switch and Telegram alerting module for the Minions paper-trading bot. Tasks are ordered to build foundational modules first (config, sender), then integrate into the existing orchestrator loop, and finally add the stretch-goal Telegram bot. Each task references specific requirements for traceability.

## Tasks

- [ ] 1. Create alerting module with config loader
  - [ ] 1.1 Create `alerting/config_loader.py` with `AlertingConfig` dataclass and `load_config()` function
    - Create the `alerting/` directory and `alerting/config_loader.py`
    - Define the `AlertingConfig` dataclass with fields: `telegram_bot_token`, `telegram_chat_id`, `enabled_events`, `telegram_bot_enabled`, `alerting_enabled`
    - Define `VALID_EVENT_TYPES` frozenset with the six recognized event types
    - Implement `load_config()` following the 9-step algorithm in the design: file existence check, YAML parse, root dict check, extract token/chat_id with env var overrides (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), determine `alerting_enabled`, validate `enabled_events`, extract `telegram_bot_enabled`
    - Function NEVER raises — always returns a config (possibly disabled)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [ ]* 1.2 Write property test for enabled_events config validation (Property 6)
    - **Property 6: enabled_events config validation retains only recognized values**
    - Generate arbitrary lists of strings (mix of valid event types and random strings)
    - Assert the returned `enabled_events` contains exactly the valid subset, in order
    - **Validates: Requirements 5.4**

  - [ ]* 1.3 Write unit tests for alerting config edge cases
    - Test alerting disabled when both credentials are empty → INFO log at init, no Telegram calls
    - Test alerting disabled when config file is missing → equivalent to empty credentials, no error
    - Test partial credentials (token set but chat_id empty) → WARNING log identifying missing chat_id, alerting disabled
    - Test partial credentials (chat_id set but token empty) → WARNING log identifying missing token, alerting disabled
    - Test env var `TELEGRAM_BOT_TOKEN` overrides YAML `telegram_bot_token` value
    - Test env var `TELEGRAM_CHAT_ID` overrides YAML `telegram_chat_id` value
    - Test malformed YAML (syntax error) → WARNING log, alerting disabled, no exception raised
    - _Requirements: 3.8, 3.9, 5.2, 5.3, 5.5, 5.6, 5.7_

  - [ ] 1.4 Create `alerting/config.yaml` template file
    - Create the default config file with commented fields and sensible defaults
    - Token and chat_id default to empty string (alerting disabled out-of-the-box)
    - `enabled_events` commented out (defaults to all six)
    - `telegram_bot_enabled: false`
    - _Requirements: 5.1, 5.3_

- [ ] 2. Implement alerting sender module
  - [ ] 2.1 Create `alerting/sender.py` with `_format_message()` and `send_alert()`
    - Implement `_format_message()`: look up emoji label, generate UTC ISO 8601 timestamp, build context lines with N/A substitution for missing fields, truncate to 4096 chars
    - Implement `send_alert()`: check config/enabled, validate event_type, format message, build Telegram API URL, HTTP POST with `urllib.request`, 5s timeout, catch all exceptions and log WARNING
    - Define module constants: `_HTTP_TIMEOUT_SECONDS`, `_MAX_MESSAGE_LENGTH`, `_EVENT_LABELS`, `_REQUIRED_CONTEXT`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.3, 4.5_

  - [ ]* 2.2 Write property test for message formatting (Property 2)
    - **Property 2: Alert message formatting includes required fields with N/A substitution**
    - For any valid event_type and any context dict (arbitrary subset of required fields), verify formatted message contains: event label, UTC ISO 8601 timestamp, and each required field value or "N/A"
    - **Validates: Requirements 3.2**

  - [ ]* 2.3 Write property test for message truncation (Property 3)
    - **Property 3: Alert message truncation to Telegram maximum**
    - For any valid event_type and context with string values up to 100,000 chars, verify final message is ≤ 4096 characters
    - **Validates: Requirements 3.3**

  - [ ]* 2.4 Write property test for send_alert exception safety (Property 4)
    - **Property 4: send_alert never raises exceptions**
    - For any input combination (valid/invalid event_type, any context dict, mocked HTTP failures), verify `send_alert()` returns None without raising
    - Mock `urllib.request.urlopen` to raise various exceptions (TimeoutError, URLError, HTTPError, RuntimeError, ValueError, OSError)
    - **Validates: Requirements 3.5, 4.2**

  - [ ]* 2.5 Write property test for unrecognized event_type rejection (Property 5)
    - **Property 5: Unrecognized event_type prevents message delivery**
    - For any string not in the six valid event types, verify no HTTP request is made (mock urllib and assert not called)
    - **Validates: Requirements 3.10**

- [ ] 3. Create alerting module public API
  - [ ] 3.1 Create `alerting/__init__.py` with `initialize()` and `send_alert()` exports
    - Implement module-level `_config` variable set by `initialize()`
    - `initialize(config_path)` calls `load_config()` and stores result
    - Public `send_alert(event_type, context)` delegates to `sender.send_alert` with the module-level config
    - Export `__all__ = ["initialize", "send_alert", "VALID_EVENT_TYPES"]`
    - _Requirements: 3.5, 4.2, 5.1_

- [ ] 4. Checkpoint — Alerting module complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Extend orchestrator config with kill_switch_file_path
  - [ ] 5.1 Add `kill_switch_file_path` field to `OrchestratorConfig` and update `load_config()`
    - Add `kill_switch_file_path: str` field to the dataclass (default `"orchestrator/KILL_SWITCH"`)
    - Add validation in `load_config()` Step 5: type must be string (reject bool), length 1–255, raise `ValueError` on violation
    - Add `"kill_switch_file_path"` to the `known_keys` set (Step 6)
    - Apply default in Step 7
    - Update the return statement to include the new field
    - _Requirements: 1.6, 7.1, 7.2, 7.3_

  - [ ]* 5.2 Write property test for kill_switch_file_path validation (Property 7)
    - **Property 7: kill_switch_file_path config validation**
    - For strings with length in [1, 255], verify `load_config` accepts them
    - For non-strings, empty strings, or strings > 255 chars, verify `ValueError` is raised
    - **Validates: Requirements 7.1, 7.2**

- [ ] 6. Integrate kill switch into orchestrator loop
  - [ ] 6.1 Implement `_check_kill_switch()` and `_interruptible_wait()` helper functions in `orchestrator/loop.py`
    - Implement `_check_kill_switch(file_path)`: uses `os.path.exists()`, returns True if file exists, catches OSError and logs WARNING returning False
    - Implement `_interruptible_wait(shutdown_event, timeout, kill_switch_file_path, check_interval=5.0)`: splits wait into 5s chunks, checks both shutdown_event and kill switch file between chunks, returns "timeout"/"shutdown"/"kill_switch"
    - _Requirements: 1.1, 1.4, 1.5, 2.6_

  - [ ]* 6.2 Write property test for kill switch file content independence (Property 1)
    - **Property 1: Kill switch triggers regardless of file content**
    - For any file content (empty bytes, random UTF-8, binary 0–10,000 bytes), create a temp file and verify `_check_kill_switch()` returns True
    - **Validates: Requirements 1.4**

  - [ ] 6.3 Implement `_kill_switch_shutdown()` and `_post_order_halt_shutdown()` helpers
    - `_kill_switch_shutdown(orch_config, state_data, cycle_number)`: log CRITICAL, send_alert, flush state, sys.exit(1); on failure log WARNING with `event="kill_switch_shutdown_failed"`, `error_type`, `error_message[:200]`, then sys.exit(2)
    - `_post_order_halt_shutdown(orch_config, state_data, cycle_number, error_context)`: send_alert, flush state, sys.exit(3); on failure log WARNING with `event="post_order_halt_shutdown_failed"`, `error_type`, `error_message[:200]`, then sys.exit(4)
    - Both use try/except pattern that re-raises SystemExit, catches Exception for dirty path
    - _Requirements: 1.2, 6.6_

  - [ ]* 6.4 Write unit tests for shutdown sequence exit codes
    - Test `_kill_switch_shutdown()` exit code 1: mock `save_state` to succeed, verify `sys.exit(1)` is called and CRITICAL log fires with `event="kill_switch_triggered"`
    - Test `_kill_switch_shutdown()` exit code 2 (dirty path): mock `save_state` to raise `OSError`, verify `sys.exit(2)` is called and WARNING log fires with `event="kill_switch_shutdown_failed"`, correct `error_type="OSError"`, and `error_message` truncated to 200 chars
    - Test `_post_order_halt_shutdown()` exit code 3: mock `save_state` to succeed, verify `sys.exit(3)` is called and `send_alert("halt_the_loop_error", ...)` is called with correct context
    - Test `_post_order_halt_shutdown()` exit code 4 (dirty path): mock `save_state` to raise `OSError`, verify `sys.exit(4)` is called and WARNING log fires with `event="post_order_halt_shutdown_failed"`, correct `error_type`, `error_message`
    - Test kill switch file is NOT deleted after trigger — file still exists on disk after `_kill_switch_shutdown()` exits
    - _Requirements: 1.2, 2.5, 6.6_

  - [ ]* 6.5 Write unit test for kill switch + Stop_Flag precedence
    - Set both: create kill switch file AND set `stop_flag = True`
    - Run the top-of-loop logic and verify `_kill_switch_shutdown()` is called (kill switch wins because it is checked first)
    - Verify graceful shutdown path is NOT reached
    - _Requirements: 2.4_

  - [ ] 6.6 Integrate kill switch check and alerting calls into `run_loop()` main loop
    - Add `import alerting` and call `alerting.initialize()` during Phase 1 initialization (wrapped in try/except — continue with alerting disabled on failure)
    - Insert `_check_kill_switch()` as FIRST operation inside `while True:`, before `stop_flag` check
    - When `_check_kill_switch()` returns True, call `_kill_switch_shutdown(orch_config, state_data, cycle_number)` — this triggers the full shutdown sequence (log CRITICAL, alert, flush, exit)
    - Replace all `shutdown_event.wait(timeout=...)` calls with `_interruptible_wait()` calls
    - Replace existing `sys.exit(1)` post-order halt paths with `_post_order_halt_shutdown()` calls
    - Add `send_alert("graceful_shutdown_completed", {"cycle_number": cycle_number})` before graceful `sys.exit(0)`
    - Add `send_alert("order_submitted", {...})` after successful order submission
    - Add `send_alert("order_filled", {...})` after order fill confirmation
    - Add `send_alert("skip_and_retry_error", {...})` on pre-order cycle skip
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [ ]* 6.7 Write integration tests for kill switch and alerting
    - Test full loop iteration with kill switch file present → verify process exits with code 1, CRITICAL log emitted, `send_alert("kill_switch_triggered", ...)` called
    - Test kill switch detected during `_interruptible_wait()` → verify function returns `"kill_switch"` before timeout elapses, and subsequent top-of-loop check triggers shutdown
    - Test end-to-end alert delivery with mocked Telegram API: mock `urllib.request.urlopen`, run a cycle that produces an order fill, verify HTTP POST to correct Telegram URL with expected message body
    - Test Orchestrator startup continues trading when `alerting.initialize()` raises an unexpected exception → verify WARNING log, verify loop enters main iteration, verify `send_alert` calls are no-ops (alerting disabled)
    - _Requirements: 1.1, 1.2, 2.6, 3.1, 3.4, 6.1_

- [ ] 7. Checkpoint — Kill switch and alerting integration complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Implement Telegram bot remote control (stretch goal)
  - [ ] 8.1 Create `alerting/telegram_bot.py` with `TelegramBotListener` class
    - Implement daemon thread that polls `getUpdates` with long-polling (30s timeout)
    - Handle `/kill` command: create kill switch file, reply with confirmation; if file already exists, reply accordingly; if creation fails, reply with error
    - Handle `/status` command: call `get_status_callback()`, format and reply with loop state
    - Handle unrecognized commands: reply with help message listing `/kill` and `/status`
    - Only accept commands from configured `telegram_chat_id` — silently ignore others
    - Self-terminate after 5 consecutive polling failures (log WARNING)
    - Thread is daemon (does not prevent process exit)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9_

  - [ ] 8.2 Wire `TelegramBotListener` into alerting initialization
    - In `alerting/__init__.py`, after `initialize()` loads config, if `telegram_bot_enabled` is True and credentials are present, instantiate and start `TelegramBotListener`
    - Pass `kill_switch_file_path` (from orchestrator config) and a status callback
    - Expose a `stop_bot()` function for clean shutdown
    - _Requirements: 8.4, 8.6_

  - [ ]* 8.3 Write unit tests for Telegram bot
    - Test `/kill` creates file, `/status` reports state
    - Test commands from unauthorized chat IDs are ignored
    - Test 5 consecutive poll failures → thread self-terminates
    - Test daemon thread attribute is set
    - _Requirements: 8.1, 8.2, 8.3, 8.5_

- [ ] 9. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (7 properties defined)
- Unit tests validate specific examples and edge cases
- The stretch goal (task 8) can be deferred entirely without affecting core kill switch and alerting functionality
- Python is the implementation language (matching existing project code and design document)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.1", "5.2"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "3.1"] },
    { "id": 3, "tasks": ["6.1"] },
    { "id": 4, "tasks": ["6.2", "6.3"] },
    { "id": 5, "tasks": ["6.4", "6.5", "6.6"] },
    { "id": 6, "tasks": ["6.7"] },
    { "id": 7, "tasks": ["8.1"] },
    { "id": 8, "tasks": ["8.2", "8.3"] }
  ]
}
```
