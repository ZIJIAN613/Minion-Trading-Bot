# Requirements Document

## Introduction

The `orchestrator` module is the fifth and final module in the Minions paper-trading pipeline. It runs the four existing modules (Data Ingestion → Strategy → Risk Engine → Execution) together in a continuous loop, turning independently-correct modules into an unattended trading bot. The Orchestrator queries Alpaca directly for account and position state at the start of every cycle (no locally cached account state), anchors its schedule to new daily bar availability rather than a fixed clock, and maintains a durable local record of positions it has opened so it can distinguish "my positions" from pre-existing ones across restarts. It handles errors differently depending on severity: data-fetch failures are retried on the next cycle, while failures after an order has been submitted halt the loop and require manual intervention. The module supports graceful shutdown by finishing the current cycle before exiting, and performs a safety check on startup to flag any open position missing a protective stop-loss order.

---

## Glossary

- **Orchestrator**: The `orchestrator/` Python module described in this document — the top-level loop that coordinates all four pipeline stages.
- **Cycle**: One complete execution of the pipeline: query account state → Data Ingestion → Strategy → Risk Engine → Execution → record outcome. A cycle is atomic from the Orchestrator's perspective — it either runs to completion or is never started.
- **Pipeline**: The ordered sequence Data Ingestion → Strategy Engine → Risk Engine → Order Execution, as defined by the project structure.
- **Account_State**: The result of querying the Alpaca paper-trading API for account information (equity, cash, buying power) and all open positions at the start of a cycle. This is always fresh-queried, never cached between cycles.
- **Position_Record**: A durable entry in the local state file representing a position the Orchestrator itself opened, containing at minimum: symbol, entry order ID, stop-loss order ID, quantity, entry timestamp, and entry price.
- **State_File**: A JSON file on disk that persists Position_Records across process restarts. This is the Orchestrator's only durable local state — account and position data is always fresh-queried from Alpaca.
- **New_Bar_Check**: The periodic check that compares the timestamp of the latest available daily bar for AAPL against the timestamp of the last bar the Orchestrator acted on. A new cycle runs only when a genuinely new bar is detected.
- **Last_Acted_Bar**: The timestamp of the most recent daily bar that triggered a full pipeline cycle. Stored in the State_File to survive restarts.
- **Safety_Check**: The startup procedure that cross-references all open positions (from Alpaca) against open stop-loss orders to identify any position missing a protective stop-loss.
- **Execution_Failure**: A Python exception that occurs after the Execution module has submitted an order to the Alpaca API (i.e., after an order ID has been returned by Alpaca). This excludes: Risk Engine vetoes, NO_ACTION decisions, Execution module returning `status="skipped"`, and any error that occurs before order submission.
- **Correct_Decline**: A pipeline outcome where no order is submitted because the Risk Engine vetoed the signal, the signal was HOLD/NO_ACTION, or the Execution module returned `status="skipped"`. These are correct decisions, not failures, and must never trigger a loop halt.
- **Stop_Flag**: A boolean flag set by the signal handler (SIGINT/SIGTERM) that instructs the Orchestrator to exit after the current cycle completes. Checked only between cycles, never mid-cycle.
- **Config_File**: The `config.yaml` file located in the `orchestrator/` directory containing configurable parameters for this module.
- **Alpaca_Paper_Client**: An instance of `alpaca.trading.TradingClient` configured exclusively for the paper-trading endpoint (`paper=True`), used to query account state and open positions/orders.

---

## Requirements

### Requirement 1: Fresh Account State Query Per Cycle

**User Story:** As a developer, I want the Orchestrator to query Alpaca for current account and position state at the start of every cycle, so that decisions are always based on the true broker state rather than stale local assumptions.

#### Acceptance Criteria

1. WHEN a new cycle begins, THE Orchestrator SHALL query the Alpaca paper-trading API for the current account information (equity, cash, buying power) and all open positions (symbol, quantity, current price, unrealized P&L) before invoking any pipeline stage.
2. THE Orchestrator SHALL format the queried data into an `account_snapshot` dictionary compatible with the Risk Engine's expected input format: `equity` (positive float), `cash` (non-negative float), `open_positions` (list of dicts each with `symbol` (string) and `size` (positive integer representing whole shares held)), and `daily_realized_pnl` (float). IF Alpaca returns a fractional quantity for a position, THEN THE Orchestrator SHALL truncate it to a whole integer (floor). IF the resulting integer is zero, THEN THE Orchestrator SHALL exclude that position from `open_positions`.
3. THE Orchestrator SHALL NOT maintain or trust any locally cached copy of account or position state between cycles — every cycle starts with a fresh query to Alpaca.
4. IF the Alpaca API does not respond within 30 seconds or returns an HTTP error status during the account state query, THEN THE Orchestrator SHALL treat this as a Data Ingestion failure (skip cycle, retry next check — see Requirement 5 criterion 1).
5. THE Orchestrator SHALL derive `daily_realized_pnl` by comparing the account's current equity against its `last_equity` value as reported by Alpaca's account endpoint (`equity - last_equity`). IF Alpaca's account object does not expose `last_equity`, THEN this criterion SHALL be revisited during Design to identify an alternative derivation, such as aggregating realized P&L from the current trading day's closed orders.
6. IF the Alpaca account endpoint returns an `equity` value that is zero or negative, THEN THE Orchestrator SHALL treat this as an error condition and skip the cycle (same behavior as criterion 4), since the Risk Engine requires `equity` to be a positive float.

---

### Requirement 2: Event-Anchored Scheduling on New Daily Bar

**User Story:** As a developer, I want the Orchestrator to run the pipeline only when a new daily bar is available, so that no cycles are wasted when the signal cannot have changed.

#### Acceptance Criteria

1. THE Orchestrator SHALL check whether a new daily bar is available for AAPL at the interval defined by `bar_check_interval_seconds`, by comparing the date portion (YYYY-MM-DD UTC) of the latest available daily bar timestamp (from the Data Ingestion module) against the Last_Acted_Bar timestamp stored in the State_File.
2. WHEN the latest available daily bar has a date strictly newer than the Last_Acted_Bar, THE Orchestrator SHALL execute one full pipeline cycle.
3. WHEN the latest available daily bar has the same date as (or is older than) the Last_Acted_Bar, THE Orchestrator SHALL idle and recheck after `bar_check_interval_seconds` elapses.
4. THE Orchestrator SHALL read `bar_check_interval_seconds` from the Config_File as a positive integer or float in the range [60, 3600], representing how many seconds to wait between new-bar checks.
5. WHEN the Orchestrator starts for the first time (no Last_Acted_Bar in the State_File or Last_Acted_Bar is null), THE Orchestrator SHALL treat the first available daily bar as new and execute a cycle immediately.
6. WHEN a cycle's pipeline execution fully resolves — meaning Execution has either completed (`"filled"`, `"partial_fill"`, `"skipped"`, `"cancelled"`, `"expired"`, `"rejected"`, or `"timeout"`) or the cycle ended in a Correct_Decline (`"vetoed"` or `"no_action"`) before reaching Execution — THE Orchestrator SHALL update the Last_Acted_Bar in the State_File to the timestamp of the bar that triggered the cycle. IF Execution is deferred due to the market being closed (criterion 9), THEN THE Orchestrator SHALL NOT update Last_Acted_Bar until Execution actually resolves after the market opens.
7. IF the new-bar check itself fails (e.g., Alpaca API unreachable or Data Ingestion raises an exception), THE Orchestrator SHALL log a WARNING-level structured event containing the error type and message, skip this check, and retry after the configured `bar_check_interval_seconds`.
8. WHEN a new daily bar is detected and a cycle is triggered, THE Orchestrator SHALL query the Alpaca clock endpoint to determine whether the market is currently open before invoking Execution. IF the market is open, THEN THE Orchestrator SHALL proceed with Execution normally.
9. IF the market is closed when a cycle reaches the Execution stage, THEN THE Orchestrator SHALL still run Data Ingestion, Strategy, and Risk Engine to compute and log the signal and decision, but SHALL defer Execution by holding the approved Decision_Dict (persisted to the State_File per criterion 12) until the market opens. THE Orchestrator SHALL NOT submit a DAY time-in-force order while the market is closed.
10. WHILE the Orchestrator is holding a deferred Decision_Dict waiting for market open, THE Orchestrator SHALL recheck the Alpaca clock endpoint at the configured `bar_check_interval_seconds` interval and invoke Execution as soon as the market is reported open — it SHALL NOT recompute the signal.
11. IF a graceful shutdown signal is received while the Orchestrator is holding a deferred Decision_Dict (market closed, awaiting open), THEN THE Orchestrator SHALL discard the deferred Decision_Dict, clear the `pending_decision` field in the State_File, log a WARNING-level event indicating that a deferred execution was abandoned due to shutdown, and exit gracefully.
12. WHEN Execution is deferred due to the market being closed (criterion 9), THE Orchestrator SHALL persist the Decision_Dict to the State_File (as a top-level field `pending_decision`) before entering the wait loop, rather than holding it only in memory. WHEN Execution subsequently resolves after market open, THE Orchestrator SHALL clear the `pending_decision` field from the State_File (set to `null`). WHEN the Orchestrator starts up and finds a non-null `pending_decision` in the State_File, THE Orchestrator SHALL log a WARNING-level structured event identifying the pending decision and SHALL resume waiting for market open to execute it, rather than silently discarding it or recomputing a new signal.

---

### Requirement 3: Full Pipeline Execution Per Cycle

**User Story:** As a developer, I want each cycle to run the complete pipeline in strict order, so that the Orchestrator produces the same decisions as manually running the four modules in sequence.

#### Acceptance Criteria

1. WHEN a cycle is triggered, THE Orchestrator SHALL execute the pipeline stages in strict order: (1) Data Ingestion — fetch historical bars, (2) Strategy — compute signal from bars, (3) Risk Engine — evaluate signal against account state, (4) Execution — place orders if approved.
2. THE Orchestrator SHALL pass outputs from each stage as inputs to the next stage without modification: bars DataFrame to Strategy, Signal_Dict to Risk Engine (along with current price and account_snapshot), Decision_Dict to Execution. IF any stage returns an unexpected type or `None` where a valid output is required, THEN THE Orchestrator SHALL treat this as a failure at that stage and skip the entire cycle (per Requirement 5 criterion 2).
3. THE Orchestrator SHALL use the close price of the most recent bar as the `current_price` argument to the Risk Engine's `evaluate_signal` function.
4. WHEN the Risk Engine returns a Decision_Dict with `decision` equal to `"VETOED"` or `"NO_ACTION"`, THE Orchestrator SHALL log an INFO-level structured event (per Requirement 10 criterion 2) and consider the cycle complete — this is a Correct_Decline, not a failure.
5. WHEN the Execution module returns an Order_Result with `status` equal to `"skipped"`, THE Orchestrator SHALL log an INFO-level structured event (per Requirement 10 criterion 2) and consider the cycle complete — this is a Correct_Decline.
6. WHEN the Execution module returns an Order_Result with `status` equal to `"filled"` or `"partial_fill"`, THE Orchestrator SHALL record the position in the State_File (see Requirement 6) using the actual filled quantity from the Order_Result, and consider the cycle complete.
7. THE Orchestrator SHALL include `symbol` set to the value of the configured `symbol` key (Requirement 8) in the signal_dict passed to the Risk Engine.
8. IF Execution is deferred because the market was closed when the cycle ran (per Requirement 2 criterion 9), THE Orchestrator SHALL re-check market status at the configured `bar_check_interval_seconds` interval and invoke Execution as soon as the market opens, using the same Decision_Dict computed earlier — it SHALL NOT recompute the signal.
9. IF a graceful shutdown signal is received while the Orchestrator is holding a deferred Decision_Dict awaiting market open, THEN THE Orchestrator SHALL discard the deferred Decision_Dict, log a WARNING-level event, and exit gracefully (per Requirement 2 criterion 11).

---

### Requirement 4: Graceful Shutdown

**User Story:** As a developer, I want the Orchestrator to finish the current cycle before exiting on a stop signal, so that no position is ever left in an unprotected state mid-order.

#### Acceptance Criteria

1. WHEN the process receives SIGINT (Ctrl+C) or SIGTERM, THE Orchestrator SHALL set the Stop_Flag to `True` and SHALL NOT interrupt the currently-executing cycle.
2. THE Orchestrator SHALL check the Stop_Flag only between cycles (after a cycle completes and before the next new-bar check begins) — never during a cycle.
3. WHEN the Stop_Flag is `True` and the current cycle has completed (or no cycle is in progress), THE Orchestrator SHALL log an INFO-level structured event (with `event` set to `"shutdown_completed"`), flush the State_File to disk using the same atomic-write mechanism defined in Requirement 6, and exit the process with exit code 0.
4. THE Orchestrator SHALL register the signal handler during initialization, before the main loop begins.
5. IF a second SIGINT/SIGTERM is received while the Orchestrator is already shutting down (Stop_Flag already `True` and waiting for cycle completion), THEN THE Orchestrator SHALL log a WARNING-level structured event containing `event` set to `"duplicate_shutdown_signal"` and the signal name, and SHALL continue waiting for the current cycle to finish — it SHALL NOT force-terminate mid-cycle.
6. WHEN the Stop_Flag is set to `True` while the Orchestrator is idle (sleeping between new-bar checks with no cycle in progress), THE Orchestrator SHALL interrupt the sleep and proceed to shutdown within 2 seconds rather than waiting for the full polling interval to elapse.
7. IF a cycle has been executing for longer than 300 seconds (5 minutes) when a shutdown is pending, THEN THE Orchestrator SHALL log a WARNING-level structured event containing `event` set to `"shutdown_cycle_timeout_warning"` and the elapsed cycle duration in seconds — the Orchestrator SHALL continue waiting (not force-terminate) but the log provides operator visibility into a potentially stuck cycle.

---

### Requirement 5: Error Handling — Severity Split

**User Story:** As a developer, I want pre-order failures to be retried automatically and post-order failures to halt the loop, so that the bot self-heals from transient issues but never leaves ambiguous positions unattended.

#### Acceptance Criteria

1. WHEN a failure occurs during Data Ingestion (network error, API unreachable, invalid response) or during the account state query (Requirement 1 criterion 4), THE Orchestrator SHALL log the error at WARNING level, skip the current cycle entirely, and continue to the next new-bar check — no order was at risk, so this is safe to self-heal.
2. WHEN a failure occurs during Strategy computation or Risk Engine evaluation (unexpected exception, not a veto), THE Orchestrator SHALL log the error at WARNING level, skip the current cycle, and continue to the next new-bar check — no order was at risk.
3. WHEN the Execution module returns an Order_Result with `status` equal to `"error"` AND the Order_Result contains an `entry_order_id` that is not `None` (meaning an order was submitted to Alpaca before the error occurred), THE Orchestrator SHALL log the error at CRITICAL level and halt the main loop immediately, requiring manual intervention.
4. WHEN the Execution module returns an Order_Result with `status` equal to `"error"` AND the Order_Result contains `entry_order_id` equal to `None` (meaning the error occurred before any order was submitted), THE Orchestrator SHALL treat this as a pre-order failure: log at WARNING level, skip the cycle, and continue to the next new-bar check.
5. WHEN the Execution module returns an Order_Result with `status` equal to `"cancelled"`, `"expired"`, `"rejected"`, or `"timeout"`, THE Orchestrator SHALL log the outcome at WARNING level and consider the cycle complete — these are terminal states where no ambiguous position exists.
6. WHEN the main loop halts due to an Execution_Failure (criterion 3), THE Orchestrator SHALL flush the State_File to disk, then exit the process with a non-zero exit code and include in the final log message the entry order ID and symbol, so the operator knows which order needs manual inspection.
7. A Risk Engine veto (`decision` = `"VETOED"`), a NO_ACTION decision, or an Execution module `status` of `"skipped"` SHALL NOT trigger an error log or loop halt under any circumstance — these are correct pipeline decisions.
8. IF the Execution module raises a raw exception (e.g., `TypeError`, `ValueError`, or a network error) without returning an Order_Result, THE Orchestrator SHALL treat this as an unknown-state failure, not a pre-order failure. THE Orchestrator SHALL log the error at CRITICAL level and halt the main loop immediately, requiring manual intervention — the same as an Execution_Failure (criterion 3) — because the Orchestrator cannot determine whether an order was submitted to Alpaca before the exception occurred. This criterion may be revisited during Design only if the Execution module's contract can guarantee that raw exceptions strictly cannot occur after any network call to Alpaca's order-submission endpoint (see the existing Design precondition on `entry_order_id` reliability).
9. IF 3 consecutive cycles are skipped due to pre-order failures (criteria 1, 2, or 4), THE Orchestrator SHALL escalate the log level from WARNING to ERROR on the third and subsequent consecutive failures, providing visibility that something is persistently broken. THE Orchestrator SHALL reset the consecutive failure counter to zero after any successful cycle completion.

---

### Requirement 6: Durable Position Record (State File)

**User Story:** As a developer, I want the Orchestrator to maintain a persistent record of positions it opened, so that it can distinguish its own positions from pre-existing ones across restarts and only trade positions it understands.

#### Acceptance Criteria

1. THE Orchestrator SHALL maintain a State_File at a configurable path (default: `orchestrator/state.json`) that persists across process restarts.
2. WHEN the Execution module returns an Order_Result with `status` equal to `"filled"` or `"partial_fill"` for a BUY signal, THE Orchestrator SHALL append a new Position_Record to the State_File containing: `symbol` (string), `entry_order_id` (string), `stop_loss_order_id` (string or null), `quantity` (integer, minimum 1 — the filled quantity), `entry_price` (float rounded to 2 decimal places — the fill price), `entry_timestamp` (UTC ISO 8601 string), and `status` (set to `"open"`).
3. WHEN the Execution module returns an Order_Result for a SELL signal whose `symbol` matches an existing open Position_Record: IF `status` is `"filled"`, THE Orchestrator SHALL set that Position_Record's `status` to `"closed"` and record the `exit_timestamp` (UTC ISO 8601 string). IF `status` is `"partial_fill"`, THE Orchestrator SHALL decrement that Position_Record's `quantity` by the filled amount and set `status` to `"partially_closed"`; IF the remaining quantity reaches zero, THEN THE Orchestrator SHALL instead set `status` to `"closed"` and record the `exit_timestamp`. THE Orchestrator SHALL log a WARNING for any `partial_fill` case, noting the remaining unclosed quantity, since a partial SELL fill leaves a residual position outside Risk Engine's full-exit assumption.
4. THE Orchestrator SHALL write the State_File atomically (write to a temporary file in the same directory as the State_File, then rename) to prevent corruption if the process is killed during a write.
5. IF the State_File does not exist on startup, THEN THE Orchestrator SHALL create a new empty State_File (with an empty positions list and `last_acted_bar` set to `null`).
6. IF the State_File exists but contains invalid JSON, THEN THE Orchestrator SHALL raise a `ValueError` with a message identifying the file path and the parse error — the Orchestrator SHALL NOT silently ignore a corrupted state file.
7. THE State_File SHALL also store the `last_acted_bar` timestamp (UTC ISO 8601 string or null) used by the scheduling logic in Requirement 2, and a `pending_decision` field (the serialized Decision_Dict or null) used by the deferred-execution logic in Requirement 2 criterion 12. WHEN no decision is deferred, `pending_decision` SHALL be `null`.
8. THE Orchestrator SHALL only make trading decisions (hold, close, react to new signals) for positions that have a corresponding open Position_Record in the State_File — pre-existing positions without a record are excluded from active trading but included in the safety check (Requirement 7).
9. IF the atomic write to the State_File fails (e.g., disk full, permission denied, rename failure), THEN THE Orchestrator SHALL log the error at CRITICAL level and halt the main loop immediately — the Orchestrator SHALL NOT continue trading if position records cannot be persisted.

---

### Requirement 7: Startup Safety Check

**User Story:** As a developer, I want the Orchestrator to verify on every startup that all open positions have a protective stop-loss, so that no position is ever held unprotected after a restart or crash.

#### Acceptance Criteria

1. WHEN the Orchestrator starts (after loading or initializing the State_File and before entering the main loop), THE Orchestrator SHALL query Alpaca for all open positions and all open orders regardless of whether the State_File was freshly created or loaded from a prior run.
2. THE Orchestrator SHALL cross-reference each open position against open stop-loss orders by matching on symbol, where a protective stop-loss is an order with `type` = `"stop"` or `"stop_limit"`, the same `symbol` as the position, and `side` = `"sell"` for a long position (positive quantity) or `side` = `"buy"` for a short position (negative quantity).
3. IF any open position does not have a corresponding protective stop-loss order, THEN THE Orchestrator SHALL log a CRITICAL-level structured event for each unprotected position, containing: `event` set to `"unprotected_position_detected"`, the symbol, the quantity, and the current market value (quantity multiplied by current price).
4. THE Orchestrator SHALL NOT automatically place a stop-loss order for an unprotected position — the Orchestrator does not know why the position exists or what stop level is appropriate. It flags loudly and continues into the main loop.
5. THE safety check SHALL apply to ALL open positions, including pre-existing ones that the Orchestrator did not open (i.e., positions without a matching Position_Record in the State_File).
6. WHEN the safety check completes, THE Orchestrator SHALL log an INFO-level structured event containing: `event` set to `"safety_check_completed"`, the total number of open positions checked, and the count of positions missing stop-loss protection.
7. IF the safety check cannot complete because the Alpaca API returns an error or does not respond within 30 seconds, THEN THE Orchestrator SHALL log a CRITICAL-level structured event containing `event` set to `"safety_check_failed"` and the error details, and SHALL halt startup with a non-zero exit code — the bot SHALL NOT begin trading without confirming position safety.

---

### Requirement 8: Configuration

**User Story:** As a developer, I want all Orchestrator parameters to be configurable via a YAML file, so that I can tune scheduling, error behavior, and file paths without modifying code.

#### Acceptance Criteria

1. WHEN the Orchestrator is initialized, THE Orchestrator SHALL read its runtime configuration from a `config.yaml` file located in the `orchestrator/` directory.
2. THE Config_File SHALL support the following keys: `bar_check_interval_seconds` (required — positive number, range [60, 3600], no default — polling interval for new-bar checks), `symbol` (optional — non-empty string, 1–10 characters, default `"AAPL"` — the ticker to trade), and `state_file_path` (optional — non-empty string, 1–255 characters, default `"orchestrator/state.json"` — relative or absolute path to the State_File).
3. IF the Config_File is absent, THEN THE Orchestrator SHALL raise a `FileNotFoundError` with a message identifying the expected file path.
4. IF the Config_File contains invalid YAML syntax, THEN THE Orchestrator SHALL raise a `ValueError` with a message describing the syntax error.
5. IF the Config_File is valid YAML but parses to a non-dictionary type, THEN THE Orchestrator SHALL raise a `ValueError` stating that the configuration must be a YAML mapping.
6. IF the Config_File is valid YAML mapping but is missing the required key `bar_check_interval_seconds`, THEN THE Orchestrator SHALL raise a `ValueError` identifying the missing key. Optional keys (`symbol`, `state_file_path`) SHALL use their default values when absent.
7. IF any configuration value fails its type or range constraint, THEN THE Orchestrator SHALL raise a `ValueError` identifying the invalid key and stating the accepted constraint (including the valid range or type).
8. IF the Config_File contains keys not recognized by the Orchestrator, THEN THE Orchestrator SHALL log a WARNING identifying each unknown key and continue startup without raising an error.

---

### Requirement 9: Paper Trading Safety Enforcement

**User Story:** As a developer, I want hard guarantees that the Orchestrator can never route operations to a live trading account, so that no bug or misconfiguration can risk real money.

#### Acceptance Criteria

1. THE Orchestrator SHALL instantiate `alpaca.trading.TradingClient` with the parameter `paper=True` as a hard-coded literal `True`, not a variable or configuration value — this client is used for account/position queries.
2. WHEN the Orchestrator is initialized, THE Orchestrator SHALL verify that the TradingClient base URL contains the substring `"paper"` (case-insensitive).
3. IF the TradingClient base URL does not contain the substring `"paper"` (case-insensitive), THEN THE Orchestrator SHALL immediately raise a `RuntimeError` with a message indicating a paper-trading safety violation, SHALL NOT perform any further initialization steps (no safety check, no state file loading, no main loop entry), and SHALL terminate the process.
4. THE Orchestrator SHALL NOT accept any configuration parameter, environment variable, or function argument that selects between paper and live trading modes.
5. THE Orchestrator SHALL reuse the `data_ingestion.credentials.load_credentials()` function to obtain API credentials — it SHALL NOT call `os.getenv`, `dotenv.load_dotenv`, or any other credential-reading mechanism directly.
6. IF `data_ingestion.credentials.load_credentials()` raises an `EnvironmentError` during Orchestrator initialization, THEN THE Orchestrator SHALL let the exception propagate unmodified, preventing startup.

---

### Requirement 10: Structured Logging

**User Story:** As a developer, I want the Orchestrator to log all significant events as structured messages, so that I can trace every cycle, decision, and error end-to-end.

#### Acceptance Criteria

1. WHEN a new cycle begins, THE Orchestrator SHALL log an INFO-level structured JSON event containing: `event` set to `"cycle_started"`, `cycle_number` (incrementing integer starting at 1, reset on each process start), the triggering bar timestamp as `bar_timestamp`, and the current account equity as `equity`.
2. WHEN a cycle completes successfully (regardless of whether a trade was placed, vetoed, or skipped), THE Orchestrator SHALL log an INFO-level structured JSON event containing: `event` set to `"cycle_completed"`, the `cycle_number`, the pipeline outcome as `outcome` (one of: `"filled"`, `"partial_fill"`, `"vetoed"`, `"no_action"`, `"skipped"`, `"cancelled"`, `"expired"`, `"rejected"`, `"timeout"`), and the `symbol`.
3. WHEN a cycle is skipped due to a pre-order failure, THE Orchestrator SHALL log a WARNING-level structured JSON event containing: `event` set to `"cycle_skipped"`, the `error_type` (Python exception class name, e.g., `"ConnectionError"`), the `error_message` (truncated to 500 characters), and the `pipeline_stage` where the failure occurred (one of: `"account_query"`, `"data_ingestion"`, `"strategy"`, `"risk_engine"`, `"execution"`).
4. WHEN the main loop halts due to an Execution_Failure, THE Orchestrator SHALL log a CRITICAL-level structured JSON event containing: `event` set to `"loop_halted"`, the `entry_order_id`, the `symbol`, the `error_details` (truncated to 500 characters), and the `cycle_number`.
5. WHEN a graceful shutdown is initiated, THE Orchestrator SHALL log an INFO-level structured JSON event containing: `event` set to `"shutdown_initiated"`, the `signal` received (e.g., `"SIGINT"` or `"SIGTERM"`), and `cycle_in_progress` (boolean indicating whether a cycle is currently executing).
6. WHEN the new-bar check detects a new bar, THE Orchestrator SHALL log a DEBUG-level structured JSON event containing: `event` set to `"new_bar_detected"`, the `bar_timestamp`, and the `previous_last_acted_bar` timestamp.
7. THE Orchestrator SHALL use the Python standard `logging` module with a logger named `"orchestrator"`.

---

### Requirement 11: Deterministic Pipeline Invocation

**User Story:** As a developer, I want the Orchestrator to invoke modules with the same interfaces and argument formats used in standalone testing, so that the integrated pipeline behaves identically to manually-run module tests.

#### Acceptance Criteria

1. THE Orchestrator SHALL invoke Data Ingestion via `RestClient.get_historical_bars(symbol, timeframe, start, end)`, using timeframe `"1d"`, with `end` set to the current date (UTC) and `start` set to at least `long_window + 1` calendar-trading-days before `end` (where `long_window` is read from `strategy/config.yaml`), ensuring the returned DataFrame contains at minimum `long_window + 1` bars for crossover computation.
2. THE Orchestrator SHALL invoke the Strategy module via `compute_signal(df, config, symbol=symbol)`, passing the bar DataFrame, a `StrategyConfig` instance loaded from `strategy/config.yaml`, and the symbol from the Orchestrator's own Config_File.
3. THE Orchestrator SHALL invoke the Risk Engine via `evaluate_signal(signal_dict, current_price, account_snapshot, config)`, passing the signal dictionary returned by the Strategy module, the close price of the last bar as `current_price`, the freshly-queried `account_snapshot`, and a `RiskConfig` instance loaded from `risk_engine/config.yaml`.
4. THE Orchestrator SHALL invoke the Execution module via `execute_decision(decision_dict, config)`, passing the Decision_Dict returned by the Risk Engine and an `ExecutionConfig` instance loaded from `execution/config.yaml`.
5. THE Orchestrator SHALL NOT modify, wrap, or re-implement any logic that belongs to the four pipeline modules — it SHALL only call their existing public functions, pass through their return values unmodified to the next stage, and load each module's config via that module's own `load_config` function.
6. IF the Data Ingestion module returns a DataFrame with fewer than `long_window + 1` rows (e.g., due to a holiday-shortened period), THEN THE Orchestrator SHALL still pass the DataFrame to the Strategy module unchanged and rely on the Strategy module's own insufficient-data handling (which returns a HOLD signal).

---

### Requirement 12: Module File Structure

**User Story:** As a developer, I want the Orchestrator to follow the same file structure pattern as other modules, so that the project remains consistent and navigable.

#### Acceptance Criteria

1. THE Orchestrator SHALL be organized in a directory named `orchestrator/` at the project root, containing at minimum: `__init__.py`, `config_loader.py`, `state_manager.py`, `safety_check.py`, `loop.py`, and `config.yaml`.
2. THE `config_loader.py` file SHALL contain an `OrchestratorConfig` dataclass and a `load_config` function that accepts a file path argument, checks file existence (raising `FileNotFoundError` if absent), parses the YAML (raising `ValueError` on syntax error), verifies all required keys are present (raising `ValueError` listing missing keys), validates each parameter's type and range (raising `ValueError` identifying the invalid key), and returns a populated `OrchestratorConfig` instance.
3. THE `state_manager.py` file SHALL contain all logic for reading, writing, and updating the State_File (Position_Records and Last_Acted_Bar).
4. THE `safety_check.py` file SHALL contain the startup safety check logic (cross-referencing positions against stop-loss orders).
5. THE `loop.py` file SHALL contain the main loop logic including new-bar checking, cycle execution, error classification, and shutdown handling.
6. THE `__init__.py` file SHALL re-export the module's public API via an `__all__` list that includes at minimum the `OrchestratorConfig` dataclass, the `load_config` function, and the main loop entry-point callable.
7. THE `config.yaml` file SHALL contain default configuration values with one explanatory comment per parameter describing its purpose and accepted range, using bare YAML mappings at the top level consistent with other module config files in this project.

---

## Design Preconditions

1. **Execution module `entry_order_id` reliability (Requirement 5)**: Before finalizing the Orchestrator's error-classification logic (Requirement 5), confirm against the Execution module's existing implementation and test suite that `entry_order_id` is reliably populated in the Order_Result whenever an entry order was actually submitted to Alpaca — including the case where the entry fills successfully but the subsequent stop-loss submission throws an exception. If Execution's current contract does not guarantee this in all failure paths, Execution may need a small amendment before Orchestrator implementation begins, rather than the Orchestrator relying on an unreliable field to decide whether to halt the loop.

---

## Open Design Items

1. **Alpaca Clock/Calendar Endpoint**: Confirm whether Alpaca's paper trading API exposes a clock/calendar endpoint (e.g., `GET /v2/clock`) that can be queried with our existing credentials to determine whether the market is currently open. The market-open check in Requirement 2 criterion 8 depends on this endpoint being available in paper trading mode.

2. **Shared polling interval for two purposes**: `bar_check_interval_seconds` is currently reused both for (a) how often to check whether a new daily bar is available, and (b) how often to recheck market-open status while an Execution is deferred. These are conceptually different cadences — a bar-availability check reasonably runs every 60–3600 seconds, but checking whether the market has opened might warrant a tighter interval near the expected open time to avoid unnecessary delay in submitting the order. Decide during Design whether to keep a single shared interval or introduce a separate configurable interval (e.g., `market_open_check_interval_seconds`) for the deferred-execution wait loop.
