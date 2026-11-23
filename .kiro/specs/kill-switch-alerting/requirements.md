# Requirements Document

## Introduction

The `kill-switch-alerting` feature adds two cross-cutting capabilities to the Minions paper-trading bot: (1) a file-based kill switch that can immediately halt the Orchestrator loop from outside the running process — checked at the start of every cycle, separate from the existing graceful shutdown — and (2) a Telegram alerting system that notifies the developer of key trading events without ever blocking or crashing the main loop. As a stretch goal, a Telegram bot can remotely trigger the kill switch and report loop status. These capabilities extend the existing Orchestrator module (orchestrator/loop.py) and add a new `alerting/` module for Telegram delivery.

---

## Glossary

- **Kill_Switch**: A file-based mechanism that halts the Orchestrator loop immediately at the start of the next cycle. The presence of a specific file on disk (the Kill_Switch_File) signals the loop to stop. This is distinct from the existing graceful shutdown (SIGINT/SIGTERM), which waits for the current cycle to finish.
- **Kill_Switch_File**: A file whose existence on disk indicates that the kill switch has been activated. Default path: `orchestrator/KILL_SWITCH`. The file's content is irrelevant — only its existence matters.
- **Alerting_Module**: The `alerting/` Python module responsible for formatting and delivering Telegram notifications. It exposes a fire-and-forget interface that never raises exceptions to callers.
- **Telegram_Alert**: A text message sent to a configured Telegram chat via the Telegram Bot API. Each alert includes a human-readable event description and a UTC timestamp.
- **Alert_Event**: One of the defined event types that triggers a Telegram notification: `order_submitted`, `order_filled`, `skip_and_retry_error`, `halt_the_loop_error`, `kill_switch_triggered`, `graceful_shutdown_completed`.
- **Telegram_Bot**: A Telegram bot (stretch goal) that listens for incoming commands (`/kill`, `/status`) and can remotely trigger the Kill_Switch or report current Orchestrator state.
- **Orchestrator**: The `orchestrator/` module that runs the main trading loop — the integration point for both the kill switch check and alert dispatch calls.
- **Stop_Flag**: The existing boolean flag set by SIGINT/SIGTERM signal handlers that instructs the Orchestrator to exit after the current cycle completes. The kill switch is a separate mechanism.

---

## Requirements

### Requirement 1: File-Based Kill Switch Detection

**User Story:** As a developer, I want the Orchestrator to check for a kill switch file at the start of every cycle, so that I can halt trading immediately from outside the process — even when I'm not at my machine.

#### Acceptance Criteria

1. WHEN the Orchestrator begins a new iteration of its main loop (before the new-bar check), THE Orchestrator SHALL check whether the Kill_Switch_File exists on disk at the configured path.
2. WHEN the Kill_Switch_File exists, THE Orchestrator SHALL execute the following shutdown sequence in order: (a) log a CRITICAL-level structured JSON event containing `event` set to `"kill_switch_triggered"` and the file path, (b) dispatch a `kill_switch_triggered` alert via the Alerting_Module, (c) flush the State_File to disk, and (d) halt the main loop with exit code 1. IF any step in the shutdown sequence itself fails (e.g., state file flush error or critical log write error), THEN THE Orchestrator SHALL halt with exit code 2 to distinguish a clean kill switch activation (exit code 1) from a shutdown sequence failure.
3. THE Orchestrator SHALL treat the kill switch check as independent from the existing graceful shutdown mechanism — the kill switch halts at the top of the loop iteration, while graceful shutdown waits for the current cycle to complete.
4. THE Orchestrator SHALL check only the existence of the Kill_Switch_File — the file's content, size, ownership, and permissions are irrelevant.
5. IF the kill switch check itself raises an OS-level exception (e.g., permission denied on the parent directory), THEN THE Orchestrator SHALL log a WARNING-level structured JSON event containing `event` set to `"kill_switch_check_failed"`, the exception type, and the exception message (truncated to 200 characters), and continue the loop iteration — a failed check SHALL NOT halt the loop or crash the process, even if the exception suggests broader file system problems that could affect trading operations.
6. THE Orchestrator SHALL read the Kill_Switch_File path from the Config_File as an optional string parameter `kill_switch_file_path` (string, 1–255 characters, default `"orchestrator/KILL_SWITCH"`).

---

### Requirement 2: Kill Switch Separation from Graceful Shutdown

**User Story:** As a developer, I want the kill switch to be a distinct mechanism from the existing SIGINT/SIGTERM shutdown, so that I have both an immediate external halt and a cycle-aware graceful stop.

#### Acceptance Criteria

1. THE Orchestrator SHALL check the Kill_Switch_File at the top of every loop iteration — before checking the Stop_Flag and before the new-bar check.
2. WHEN the Kill_Switch_File exists at the time of the top-of-loop check, THE Orchestrator SHALL halt without waiting for any in-progress cycle to complete and without performing Stop_Flag-style graceful shutdown steps — but the kill switch's own shutdown sequence (logging, alert dispatch, and State_File flush as defined in Requirement 1, criterion 2) SHALL still execute before the process halts. The check occurs only between cycles (at the top of the loop), ensuring no cycle is interrupted mid-execution.
3. THE kill switch mechanism SHALL NOT modify, set, or interact with the existing Stop_Flag used by the graceful shutdown signal handler.
4. IF both the Kill_Switch_File exists and the Stop_Flag is set when the top-of-loop check executes, THEN THE Orchestrator SHALL honor the kill switch (halt with exit code 1) because the kill switch check precedes the Stop_Flag check.
5. THE Orchestrator SHALL NOT automatically delete or modify the Kill_Switch_File after triggering — the developer must manually remove the file to re-enable trading.
6. WHILE the Orchestrator is idle (sleeping between new-bar checks), THE Orchestrator SHALL wake up immediately when the Kill_Switch_File is detected rather than waiting for the full sleep interval to expire — the kill switch check SHALL be performed periodically during sleep or the sleep SHALL be interruptible to minimize response latency.

---

### Requirement 3: Telegram Alert Delivery

**User Story:** As a developer, I want to receive Telegram notifications on key trading events, so that I can monitor the bot's activity without watching logs.

#### Acceptance Criteria

1. THE Alerting_Module SHALL send a Telegram message for each of the following Alert_Events: `order_submitted` (when Execution sends an order to Alpaca), `order_filled` (when Execution confirms a fill or partial fill), `skip_and_retry_error` (when a pre-order failure skips a cycle), `halt_the_loop_error` (when a post-order failure halts the loop), `kill_switch_triggered` (when the kill switch file is detected), and `graceful_shutdown_completed` (when the Orchestrator exits cleanly via Stop_Flag).
2. WHEN an Alert_Event occurs, THE Alerting_Module SHALL format the message with: the event type as a fixed label mapped from the event_type string (e.g., `order_submitted` → "Order Submitted"), the UTC timestamp in ISO 8601 format, and the following context fields per event type: `order_submitted` requires `symbol` and `order_id`; `order_filled` requires `symbol`, `order_id`, `filled_quantity`, and `fill_price`; `skip_and_retry_error` requires `error_summary` and `cycle_number`; `halt_the_loop_error` requires `error_summary`, `symbol`, and `order_id`; `kill_switch_triggered` requires `cycle_number`; `graceful_shutdown_completed` requires `cycle_number`. IF a required context field is missing from the provided `context` dict, THEN THE Alerting_Module SHALL substitute the string "N/A" for that field.
3. THE Alerting_Module SHALL truncate the final formatted message to 4096 characters (the Telegram API maximum) before sending, to prevent API rejection due to message length.
4. THE Alerting_Module SHALL send messages via the Telegram Bot API's `sendMessage` endpoint using an HTTP POST request with the configured bot token and chat ID.
5. THE Alerting_Module SHALL expose a single public function `send_alert(event_type: str, context: dict) -> None` that callers use to dispatch alerts. This function SHALL NOT raise any exception — it catches all errors internally.
6. IF the Telegram API call fails (network error, timeout, HTTP error, invalid token), THEN THE Alerting_Module SHALL log the failure at WARNING level and return silently — alert delivery failure SHALL NOT crash or block the main loop.
7. THE Alerting_Module SHALL enforce a timeout of 5 seconds on each Telegram API call to prevent blocking the main loop on slow network conditions.
8. IF both `telegram_bot_token` and `telegram_chat_id` are unconfigured (empty or absent), THEN THE Alerting_Module SHALL silently skip all alert delivery without logging a warning on every call and without attempting any Telegram API interaction — a single INFO-level log at initialization indicating alerting is disabled is sufficient.
9. IF exactly one of `telegram_bot_token` or `telegram_chat_id` is configured while the other is empty or absent, THEN THE Alerting_Module SHALL log a single WARNING at initialization indicating incomplete Telegram configuration and identifying which value is missing, and SHALL skip all alert delivery (same behavior as both unconfigured).
10. IF `send_alert` is called with an `event_type` string that is not one of the six defined Alert_Events, THEN THE Alerting_Module SHALL log the event at WARNING level identifying the unrecognized event_type and SHALL NOT send a Telegram message.

---

### Requirement 4: Alert Delivery Isolation

**User Story:** As a developer, I want alert delivery to never interfere with the trading loop, so that a Telegram outage or misconfiguration cannot affect trading operations.

#### Acceptance Criteria

1. THE Alerting_Module's `send_alert` function SHALL complete within 10 seconds under all circumstances — the 5-second HTTP timeout SHALL apply to the entire HTTP operation (DNS resolution, connection establishment, and response read combined), with the remaining budget covering message formatting and logging overhead.
2. IF the `send_alert` function encounters any exception (including unexpected runtime errors, `KeyboardInterrupt` excluded), THEN the function SHALL catch the exception, log it at WARNING level including the exception type and message (truncated to 200 characters), and return `None` — it SHALL NOT propagate exceptions to the caller.
3. THE Alerting_Module SHALL NOT hold any resources (open sockets, threads, or connections) between alert calls — each call SHALL create a new HTTP connection and release it before returning, regardless of success or failure.
4. THE Orchestrator SHALL call `send_alert` synchronously (blocking for up to the timeout) to maintain code simplicity and explicitness — asynchronous or background-thread delivery is not required for a bot that runs cycles at multi-minute intervals. A single cycle SHALL invoke `send_alert` at most 2 times (one for `order_submitted` and one for `order_filled`, or one error alert), bounding the maximum aggregate blocking time per cycle to 20 seconds in the worst case.
5. IF the Alerting_Module is disabled (no credentials configured), THEN `send_alert` SHALL return `None` without performing any I/O operations, network calls, or file access — only an in-memory credential check and immediate return. WHEN the Alerting_Module is enabled, I/O operations (network calls to Telegram API) are permitted as part of normal alert delivery.

---

### Requirement 5: Alerting Configuration

**User Story:** As a developer, I want to configure Telegram alerting via a YAML file and environment variables, so that I can enable or disable alerting without modifying code.

#### Acceptance Criteria

1. THE Alerting_Module SHALL read its configuration from `alerting/config.yaml` containing: `telegram_bot_token` (optional string, default empty — the Telegram Bot API token), `telegram_chat_id` (optional string, default empty — the Telegram chat ID to send messages to), and `enabled_events` (optional list of strings, default all six event types — which events trigger alerts). Configuration SHALL be loaded once during Alerting_Module initialization and remain fixed for the lifetime of the process.
2. IF `telegram_bot_token` or `telegram_chat_id` is empty or absent, THEN THE Alerting_Module SHALL consider alerting disabled and log a single INFO-level message at initialization.
3. IF `alerting/config.yaml` does not exist, THEN THE Alerting_Module SHALL treat this as alerting disabled (equivalent to empty credentials) — it SHALL NOT raise an error or prevent Orchestrator startup. IF the config file exists but all credential fields are empty, THE Alerting_Module SHALL follow the normal empty credential rules (Requirement 3, criterion 8).
4. THE Alerting_Module SHALL validate that `enabled_events` (if present) contains only the six recognized event type strings: `order_submitted`, `order_filled`, `skip_and_retry_error`, `halt_the_loop_error`, `kill_switch_triggered`, and `graceful_shutdown_completed`. IF an unrecognized event type is listed, THEN THE Alerting_Module SHALL log a WARNING identifying the unrecognized value and ignore it (only recognized values in the list are applied) — alerting SHALL remain enabled for the valid event types.
5. WHERE the `telegram_bot_token` contains sensitive credentials, THE Alerting_Module SHALL support reading the token value from an environment variable `TELEGRAM_BOT_TOKEN` as an alternative to the YAML file. IF both the environment variable and the YAML value are set, THE environment variable SHALL take precedence.
6. WHERE the `telegram_chat_id` is preferred from an environment variable, THE Alerting_Module SHALL support reading from `TELEGRAM_CHAT_ID`. IF both the environment variable and the YAML value are set, THE environment variable SHALL take precedence.
7. IF `alerting/config.yaml` exists but contains malformed YAML (syntax errors or non-mapping root structure), THEN THE Alerting_Module SHALL log a WARNING-level message identifying the parse error, treat alerting as disabled, and SHALL NOT raise an error or prevent Orchestrator startup.

---

### Requirement 6: Orchestrator Integration Points

**User Story:** As a developer, I want the kill switch check and alert calls to be clearly integrated into the existing Orchestrator loop, so that the additions are minimal and do not disrupt existing logic.

#### Acceptance Criteria

1. WHEN the Orchestrator initializes, THE Orchestrator SHALL initialize the Alerting_Module (load config, validate credentials). IF initialization raises any exception (file I/O error, YAML parse error, or unexpected runtime error), THE Orchestrator SHALL log a WARNING and continue startup with alerting disabled — alerting failure SHALL NOT prevent the bot from trading.
2. THE Orchestrator SHALL insert the kill switch file check as the first operation inside the main loop, before checking the Stop_Flag and before the new-bar check.
3. WHEN the Execution module returns an Order_Result with `status` equal to `"filled"` or `"partial_fill"`, THE Orchestrator SHALL call `send_alert("order_filled", {...})` with context including the symbol, filled quantity, fill price, and order ID.
4. WHEN the `execute_decision()` function returns an Order_Result with `status` equal to `"submitted"`, THE Orchestrator SHALL call `send_alert("order_submitted", {...})` with context including the symbol, side (BUY/SELL), quantity, and order type.
5. WHEN a pre-order failure causes a cycle skip, THE Orchestrator SHALL call `send_alert("skip_and_retry_error", {...})` with context including the pipeline stage, error type, and error message (truncated to 200 characters).
6. WHEN a post-order failure halts the loop, THE Orchestrator SHALL execute the following shutdown sequence in order: (a) call `send_alert("halt_the_loop_error", {...})` with context including the entry order ID, symbol, and error detail (truncated to 200 characters), (b) flush the State_File to disk, and (c) halt the main loop with exit code 3. IF the shutdown sequence itself fails (e.g., state file flush error or log write error), THEN THE Orchestrator SHALL halt with exit code 4 to distinguish a clean post-order halt (exit code 3) from a shutdown sequence failure. This mirrors the alert-then-flush-then-halt ordering defined for the kill switch in Requirement 1, criterion 2.
7. WHEN the Orchestrator exits cleanly via graceful shutdown, THE Orchestrator SHALL call `send_alert("graceful_shutdown_completed", {...})` with context including the total cycles completed during this run.
8. THE Orchestrator SHALL be able to halt and flush state for other reasons (e.g., post-order failures) without dispatching a `kill_switch_triggered` alert.

---

### Requirement 7: Kill Switch Configuration

**User Story:** As a developer, I want the kill switch file path to be configurable, so that I can place it wherever is most convenient for my deployment.

#### Acceptance Criteria

1. THE Orchestrator Config_File SHALL support an optional key `kill_switch_file_path` (string, 1–255 characters, default `"orchestrator/KILL_SWITCH"`) specifying the path to the Kill_Switch_File.
2. IF `kill_switch_file_path` is present but is not a string, is an empty string, or exceeds 255 characters, THEN THE Orchestrator SHALL raise a `ValueError` identifying the invalid key and stating the constraint (must be a non-empty string of 1–255 characters), and SHALL NOT use the invalid path for any subsequent file operations — the Orchestrator SHALL not start the main loop with an invalid kill switch path.
3. IF `kill_switch_file_path` is an absolute path, THEN THE Orchestrator SHALL use it as-is without prepending the working directory. IF `kill_switch_file_path` is a relative path, THEN THE Orchestrator SHALL resolve it relative to the current working directory (same as `state_file_path` behavior in the existing Orchestrator config).

---

### Requirement 8: Telegram Bot Remote Control (Stretch Goal)

**User Story:** As a developer, I want a Telegram bot that can trigger the kill switch and report status remotely, so that I can control the bot from my phone when I'm away from my machine.

#### Acceptance Criteria

1. WHERE the Telegram Bot remote control feature is enabled, THE Telegram_Bot SHALL listen for incoming `/kill` commands and create the Kill_Switch_File at the configured path when received. IF the Kill_Switch_File already exists when `/kill` is received, THE Telegram_Bot SHALL reply indicating the kill switch is already active and take no further action.
2. WHERE the Telegram Bot remote control feature is enabled, THE Telegram_Bot SHALL listen for incoming `/status` commands and reply with the current loop state: whether the loop is running, the last cycle number, the last cycle outcome, and whether the kill switch file currently exists.
3. THE Telegram_Bot SHALL only accept commands from the configured `telegram_chat_id` — commands from other chat IDs SHALL be ignored silently.
4. THE Telegram_Bot SHALL run in a separate daemon thread from the main Orchestrator loop, so that command processing does not interfere with trading cycle timing and the thread does not prevent process exit.
5. IF the Telegram_Bot thread encounters 5 consecutive polling failures (e.g., network errors, HTTP errors), THEN the Telegram_Bot thread SHALL log the error at WARNING level and terminate itself — it SHALL NOT crash or affect the main Orchestrator process.
6. THE Telegram_Bot feature SHALL be disabled by default. It SHALL be enabled only when `telegram_bot_enabled` is set to `true` in `alerting/config.yaml` AND both `telegram_bot_token` and `telegram_chat_id` are non-empty strings.
7. WHEN the `/kill` command is received and the Kill_Switch_File is successfully created, THE Telegram_Bot SHALL reply with a confirmation message to the chat indicating that the kill switch has been activated.
8. IF an authorized `/kill` command fails to create the Kill_Switch_File (e.g., permission denied), THEN THE Telegram_Bot SHALL reply with an error message describing the failure. Commands from unauthorized chat IDs SHALL be ignored silently without any error response.
9. IF the Telegram_Bot receives an unrecognized command (not `/kill` or `/status`), THE Telegram_Bot SHALL reply with a help message listing the supported commands.
