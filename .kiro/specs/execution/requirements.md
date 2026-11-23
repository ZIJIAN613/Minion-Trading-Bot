# Requirements Document

## Introduction

The `execution` module is the fourth and final active stage in the Minions paper-trading pipeline (Data Ingestion → Strategy Engine → Risk Engine → Order Execution). It is the ONLY module permitted to place, modify, or cancel orders. The module receives a Decision_Dict from the `risk_engine` module, validates that the decision has been approved, and places the corresponding order via the Alpaca paper-trading API. It submits both an entry order and a companion stop-loss order for every approved trade, confirms order fills, and reports all activity (submissions, fills, rejections, errors) to logging. This module is restricted to Alpaca paper trading — no code path may route to a live brokerage account under any circumstance.

---

## Glossary

- **Execution_Module**: The `execution/` Python module described in this document — the sole component authorized to place, modify, or cancel orders.
- **Decision_Dict**: The structured dictionary produced by the `risk_engine` module containing `decision` (one of `"APPROVED"`, `"VETOED"`, `"NO_ACTION"`), `signal` (`"BUY"`, `"SELL"`, `"HOLD"`), `position_size` (non-negative integer), `stop_loss` (float rounded to 2 decimals or `None`), `reason` (non-empty string 1–500 chars), `timestamp` (UTC-aware pandas Timestamp), and optionally `symbol` (string).
- **Order_Result**: The structured dictionary returned by the Execution_Module after processing a Decision_Dict, containing details about the submitted order(s), fill status, and any errors encountered.
- **Entry_Order**: The primary market order placed to open or close a position as directed by the Decision_Dict. This is the market leg of the bracket order.
- **Stop_Loss_Order**: The stop-loss leg of the bracket order, automatically linked to the Entry_Order by Alpaca, triggered at the price specified by `Decision_Dict["stop_loss"]`.
- **Bracket_Order**: An Alpaca order class that combines an entry order with an attached stop-loss order as a single atomic submission, ensuring every position is opened with a predefined exit point.
- **Alpaca_Paper_Client**: An instance of `alpaca.trading.TradingClient` configured exclusively for the paper-trading endpoint (`paper=True`). This is the only trading client the Execution_Module may instantiate.
- **Fill_Confirmation**: The acknowledgment from the Alpaca API that an order has been completely or partially filled, including fill price and quantity.
- **Order_Status**: The current state of an order as reported by Alpaca (e.g., `"new"`, `"filled"`, `"partially_filled"`, `"cancelled"`, `"rejected"`).
- **Config_File**: The `config.yaml` file located in the `execution/` directory containing configurable execution parameters.
- **Execution_Config**: A validated dataclass holding all runtime configuration for the Execution_Module.
- **Pipeline**: The ordered sequence Data Ingestion → Strategy Engine → Risk Engine → Order Execution.
- **Base_URL**: The Alpaca API endpoint URL — must always resolve to the paper-trading environment (`https://paper-api.alpaca.markets`).

---

## Requirements

### Requirement 1: Decision Input Validation

**User Story:** As a developer, I want the Execution Module to validate incoming Decision_Dicts before acting on them, so that only properly risk-approved decisions can trigger order placement.

#### Acceptance Criteria

1. WHEN `execute_decision(decision_dict)` is called, THE Execution_Module SHALL verify that `decision_dict` is a Python dictionary; IF `decision_dict` is not a dictionary (including `None`), THEN THE Execution_Module SHALL raise a `TypeError` whose message states the actual type received.
2. WHEN `decision_dict` is a dictionary, THE Execution_Module SHALL verify that it contains the key `decision` with a value that is one of `"APPROVED"`, `"VETOED"`, or `"NO_ACTION"`; IF the key is missing or the value is not one of those three strings, THEN THE Execution_Module SHALL raise a `ValueError` whose message identifies the invalid or missing key, and SHALL skip all subsequent validation steps.
3. WHEN `decision_dict` is a dictionary with a valid `decision` value, THE Execution_Module SHALL verify that it contains the key `signal` with a value that is one of `"BUY"`, `"SELL"`, or `"HOLD"`; IF the key is missing or the value is not one of those three strings, THEN THE Execution_Module SHALL raise a `ValueError` whose message identifies the invalid or missing key. Signal validation SHALL only be performed after the `decision` field is confirmed valid.
4. WHEN `decision_dict` is a dictionary with valid `decision` and `signal` values, THE Execution_Module SHALL verify that it contains `timestamp` as a timezone-aware pandas Timestamp and `reason` as a non-empty string of 1–500 characters; IF either is missing or invalid, THEN THE Execution_Module SHALL raise a `ValueError` whose message identifies the first invalid or missing field (checking `timestamp` before `reason`). Timestamp/reason validation SHALL only be performed after `decision` and `signal` are confirmed valid.
5. WHEN `decision_dict` is a dictionary with `decision` equal to `"APPROVED"` and all prerequisite fields (`decision`, `signal`, `timestamp`, `reason`) are confirmed valid, THE Execution_Module SHALL verify that `position_size` is present and is a positive integer (≥ 1, with boolean values explicitly rejected) and that `stop_loss` is present and is a positive float (> 0.0, with boolean values explicitly rejected); IF either key is missing, either value is a boolean, or either value fails its numeric constraint, THEN THE Execution_Module SHALL raise a `ValueError` whose message identifies the first invalid field encountered (checking `position_size` before `stop_loss`).
6. WHEN `decision_dict` is a dictionary with `decision` equal to `"APPROVED"` and all prerequisite fields (including `position_size` and `stop_loss`) are confirmed valid, THE Execution_Module SHALL verify that `symbol` is present and is a non-empty string of 1–10 characters; IF `symbol` is missing or invalid, THEN THE Execution_Module SHALL raise a `ValueError` whose message states that a symbol is required for order execution.
7. THE Execution_Module SHALL perform validation checks in the strict order defined by criteria 1 through 6 (type check → `decision` key → `signal` key → `timestamp`/`reason` → `position_size`/`stop_loss` → `symbol`), raising on the first failure encountered (fail-fast), so that when multiple fields are invalid only the first violation in this sequence is reported. Each step SHALL only execute if all preceding steps have passed.

---

### Requirement 2: Non-Approved Decision Handling

**User Story:** As a developer, I want the Execution Module to never receive VETOED or NO_ACTION decisions as actionable inputs, so that only risk-approved trades reach the order submission logic.

#### Acceptance Criteria

1. WHEN `execute_decision` is called with a Decision_Dict where `decision` is `"VETOED"`, THE Execution_Module SHALL NOT place any order with the Alpaca API, SHALL log an INFO-level structured event containing `event` set to `"decision_skipped"`, the `decision` value, `signal`, `symbol` (if present in the Decision_Dict), and `reason`, and SHALL return an Order_Result with `status` set to `"skipped"`, `decision` set to `"VETOED"`, `signal` set to the original signal value, `symbol` set to the Decision_Dict symbol or `None`, `position_size` set to `0`, `reason` set to `"Decision was VETOED by risk engine"`, and `timestamp` set to the original Decision_Dict timestamp. IF any order is inadvertently placed for a VETOED decision, THE Execution_Module SHALL raise a `RuntimeError` indicating an invariant violation — this check SHALL be implemented as an explicit `if ... raise RuntimeError(...)` statement (not a bare `assert`) so that it is unconditionally enforced regardless of Python optimization flags.
2. WHEN `execute_decision` is called with a Decision_Dict where `decision` is `"NO_ACTION"`, THE Execution_Module SHALL NOT place any order with the Alpaca API, SHALL log an INFO-level structured event containing `event` set to `"decision_skipped"`, the `decision` value, `signal`, `symbol` (if present in the Decision_Dict), and `reason`, and SHALL return an Order_Result with `status` set to `"skipped"`, `decision` set to `"NO_ACTION"`, `signal` set to the original signal value, `symbol` set to the Decision_Dict symbol or `None`, `position_size` set to `0`, `reason` set to `"No action required (HOLD signal)"`, and `timestamp` set to the original Decision_Dict timestamp.
3. THE Execution_Module SHALL treat VETOED and NO_ACTION decisions as non-actionable — logging and returning are the only permitted actions for these decisions. Specifically, the Execution_Module SHALL NOT instantiate, invoke, or interact with the Alpaca_Paper_Client for non-approved decisions.

---

### Requirement 3: Order Placement (BUY = Bracket, SELL = Plain Market)

**User Story:** As a developer, I want BUY signals to submit a bracket order (market entry + stop-loss) so that every new position is opened with an automatic loss-limiting exit, and I want SELL signals to submit a plain market sell order (no stop-loss) because SELL always closes an existing position and attaching a stop-loss buy would cause an unintended re-entry.

#### Acceptance Criteria

1. WHEN `execute_decision` is called with a Decision_Dict where `decision` is `"APPROVED"` and `signal` is `"BUY"`, THE Execution_Module SHALL submit a bracket order to Alpaca consisting of a market buy order for the symbol specified in `decision_dict["symbol"]` with quantity equal to `decision_dict["position_size"]`, with an attached stop-loss sell order at `decision_dict["stop_loss"]`.
2. WHEN `execute_decision` is called with a Decision_Dict where `decision` is `"APPROVED"` and `signal` is `"SELL"`, THE Execution_Module SHALL submit a plain market sell order to Alpaca for the symbol specified in `decision_dict["symbol"]` with quantity equal to `decision_dict["position_size"]`, with NO attached stop-loss order and NO bracket order class — SELL signals close existing positions and must not create any mechanism that would re-enter the position.
3. THE Execution_Module SHALL use `time_in_force` of `"day"` for the market entry order (both BUY bracket and SELL plain).
4. WHEN the signal is `"BUY"`, THE Execution_Module SHALL use `time_in_force` of `"gtc"` (good-till-cancelled) for the stop-loss leg of the bracket order.
5. WHEN the Alpaca API returns a successful order submission response (HTTP 200/201) containing an order ID, THE Execution_Module SHALL record the entry order ID in the Order_Result; for BUY signals, it SHALL additionally record the stop-loss order ID; for SELL signals, `stop_loss_order_id` SHALL be set to `None`.
6. WHEN `execute_decision` is called with a Decision_Dict where `decision` is `"APPROVED"` and `signal` is `"HOLD"`, THE Execution_Module SHALL NOT place any order and SHALL return an Order_Result with `status` set to `"skipped"` and `reason` set to `"HOLD signal cannot be executed"` — this guards against a malformed Decision_Dict that claims APPROVED for a HOLD signal.
7. THE Execution_Module SHALL ignore the `decision_dict["stop_loss"]` value for SELL signals — it is passed through from the risk engine but is not used for order placement when closing a position.

---

### Requirement 4: Stop-Loss Order as Part of BUY Bracket

**User Story:** As a developer, I want the stop-loss to be submitted as an integral part of the BUY bracket order rather than a separate independent order, so that the two legs are atomically linked by Alpaca for new positions only.

#### Acceptance Criteria

1. WHEN the signal is `"BUY"`, THE Execution_Module SHALL submit the stop-loss as the stop-loss leg of an Alpaca bracket order (using `order_class="bracket"` or equivalent API mechanism), ensuring the stop-loss is automatically linked to the entry order by the broker.
2. THE Execution_Module SHALL set the stop-loss price to the value in `decision_dict["stop_loss"]` (already rounded to 2 decimal places by the risk engine).
3. IF the Alpaca API rejects a BUY bracket order submission with an error response indicating the symbol or order configuration is ineligible for bracket orders, THEN THE Execution_Module SHALL fall back to submitting the entry buy order as a plain market order first, waiting until the entry order reaches `"filled"` or `"partially_filled"` status (meaning shares are actually held), and only then submitting the stop-loss as a separate stop order for the filled quantity at the same stop-loss price, logging a WARNING-level structured event containing the symbol, the rejection reason from the API, and the fallback action taken. The stop-loss SHALL NOT be submitted merely upon receiving an order ID from Alpaca — it SHALL only be submitted after shares are confirmed held (entry order status is `"filled"` or `"partially_filled"`).
4. WHEN the stop-loss is submitted as part of a bracket order, THE Execution_Module SHALL record the stop-loss order ID from the Alpaca response in the Order_Result.
5. WHEN the stop-loss is submitted as a separate order via the fallback path, THE Execution_Module SHALL record the stop-loss order ID in the Order_Result and set a field `bracket_fallback` to `True`.
6. IF the fallback path successfully submits the entry order but the subsequent stop-loss order submission fails, THEN THE Execution_Module SHALL log an ERROR-level structured event containing the entry order ID and the stop-loss submission error, and SHALL return an Order_Result with `status` set to `"error"` and `reason` indicating that the entry order was placed but the stop-loss could not be attached.
7. WHEN the signal is `"SELL"`, THE Execution_Module SHALL NOT submit any stop-loss order (neither as a bracket leg nor as a separate order) — SELL signals close positions and require no protective exit.

---

### Requirement 5: Order Fill Confirmation and Partial Fill Handling

**User Story:** As a developer, I want the Execution Module to wait for the order to reach a terminal state before returning, and to handle partial fills by accepting the filled quantity as final after a timeout.

#### Acceptance Criteria

1. WHEN an order is submitted (bracket for BUY, plain market for SELL), THE Execution_Module SHALL poll the Alpaca API for the entry order status by its order ID at intervals of `poll_interval_seconds` (as defined in the Execution_Config) until the order reaches a terminal state (`"filled"`, `"partially_filled"`, `"cancelled"`, `"expired"`, or `"rejected"`), up to a maximum duration of `fill_timeout_seconds` (as defined in the Execution_Config).
2. WHEN the Entry_Order reaches `"filled"` status, THE Execution_Module SHALL record the fill price and filled quantity in the Order_Result and set `status` to `"filled"`.
3. WHEN the Entry_Order is `"partially_filled"` and `fill_timeout_seconds` has elapsed, THE Execution_Module SHALL accept the filled quantity as final, cancel any remaining unfilled portion of the entry order, and set the Order_Result `status` to `"partial_fill"` with `filled_quantity` reflecting only the shares actually filled. THE Execution_Module SHALL complete the partial fill processing (cancellation and Order_Result construction) even if stop-loss verification encounters issues. For BUY signals only, THE Execution_Module SHALL additionally verify the stop-loss order's quantity matches the actual filled quantity (and cancel and resubmit a corrected stop-loss if it does not); IF the stop-loss correction fails, THE Execution_Module SHALL log the failure at WARNING level but still return the partial fill Order_Result rather than blocking.
4. WHEN the Entry_Order reaches `"cancelled"`, `"expired"`, or `"rejected"` status before any shares are filled, THE Execution_Module SHALL record the terminal status in the Order_Result, set `status` to the terminal state string, and log the event at WARNING level. For BUY signals only, THE Execution_Module SHALL verify that the associated stop-loss order has also been cancelled (and explicitly cancel it if still active).
5. IF the fill confirmation polling exceeds `fill_timeout_seconds` without the order reaching any terminal state, THEN THE Execution_Module SHALL query the order by ID one final time, and if still not terminal, cancel the order, set the Order_Result `status` to `"timeout"`, and log a WARNING.
6. THE Execution_Module SHALL include a `filled_at` timestamp (UTC-aware) in the Order_Result when the order is filled or partially filled, representing the timestamp reported by the Alpaca API for the fill event; IF the Alpaca response does not include a fill timestamp, THEN THE Execution_Module SHALL use `datetime.now(datetime.timezone.utc)` (a timezone-aware UTC datetime, never a naive `datetime.now()`) as the fallback value, consistent with all other timestamp fields in the pipeline.
7. THE Execution_Module SHALL NOT consider order submission alone as success — only a terminal state (filled, partial_fill, cancelled, expired, rejected, timeout) constitutes a completed execution attempt.

---

### Requirement 6: Paper Trading Safety Enforcement

**User Story:** As a developer, I want hard guarantees that the Execution Module can never route orders to a live trading account, so that no bug or misconfiguration can risk real money.

#### Acceptance Criteria

1. THE Execution_Module SHALL instantiate `alpaca.trading.TradingClient` with the parameter `paper=True` — this parameter SHALL be a hard-coded literal `True`, not a variable or configuration value.
2. WHEN the Execution_Module is initialized, THE Execution_Module SHALL retrieve the base URL from the instantiated `TradingClient` object and verify that it contains the substring `"paper"` (case-insensitive check); IF the base URL does not contain `"paper"` (even if `paper=True` was correctly set), THEN THE Execution_Module SHALL raise a `RuntimeError` with a message indicating that live trading is not permitted and SHALL NOT proceed with any order operations.
3. THE Execution_Module SHALL NOT accept any configuration parameter, environment variable, or function argument that selects between paper and live trading modes — the paper mode SHALL be unconditionally enforced.
4. WHEN a static AST scan is performed on all `.py` source files within the `execution/` directory, THE scan SHALL confirm that the string `paper=True` appears in every `TradingClient` instantiation and that no instantiation uses `paper=False` or omits the `paper` parameter.
5. THE Execution_Module `__init__.py` file SHALL include a module-level docstring stating that live trading is forbidden, and the source file that instantiates the `TradingClient` SHALL include at least one inline comment adjacent to the `paper=True` literal stating that this setting must never be changed.

---

### Requirement 7: Structured Logging and Traceability

**User Story:** As a developer, I want the Execution Module to log all activity as structured events including the Alpaca order ID, so that every order submission, fill, rejection, and error is traceable end-to-end.

#### Acceptance Criteria

1. WHEN an order is submitted to Alpaca, THE Execution_Module SHALL log an INFO-level structured event containing: `event` type (`"order_submitted"`), order type (market/stop/bracket), side (buy/sell), symbol, quantity, Alpaca order ID, and the Decision_Dict timestamp for correlation.
2. WHEN an order fill is confirmed, THE Execution_Module SHALL log an INFO-level structured event containing: `event` type (`"order_filled"`), symbol, filled quantity, fill price, Alpaca order ID, and `filled_at` timestamp.
3. WHEN a partial fill is accepted, THE Execution_Module SHALL log an INFO-level structured event containing: `event` type (`"partial_fill_accepted"`), symbol, filled quantity, remaining cancelled quantity, Alpaca order ID, and timeout duration in seconds.
4. WHEN an order reaches a non-filled terminal state (cancelled, expired, rejected), THE Execution_Module SHALL log a WARNING-level structured event containing: `event` type (`"order_terminal"`), symbol, Alpaca order ID, terminal status, and any rejection reason from the API (or `None` if no reason is provided).
5. WHEN an API error occurs during order submission or status polling, THE Execution_Module SHALL log an ERROR-level structured event containing: `event` type (`"api_error"`), error type (exception class name), error message (truncated to 500 characters if longer), symbol, and Alpaca order ID (or `None` if the order was not yet submitted).
6. WHEN a non-approved decision is skipped, THE Execution_Module SHALL log an INFO-level structured event containing: `event` type (`"decision_skipped"`), decision type (`"VETOED"` or `"NO_ACTION"`), signal, symbol (or `None` if not present in the Decision_Dict), and reason.
7. THE Execution_Module SHALL use the Python standard `logging` module with a logger named `"execution"`, consistent with the project's logging conventions.
8. THE Execution_Module SHALL include the Alpaca order ID in every log event that occurs after the Alpaca API returns a successful order submission response, enabling tracing of a single order across all subsequent log entries for that execution attempt.
9. IF logging raises an exception during any log call, THEN THE Execution_Module SHALL catch the exception and attempt to write a fallback notification to `sys.stderr` containing the original log message and the logging exception details, then continue execution without interruption — a logging failure SHALL NOT prevent order placement, fill confirmation, or Order_Result return.

---

### Requirement 8: Configuration

**User Story:** As a developer, I want all execution parameters to be configurable via a YAML file, so that I can tune timeouts and order behavior without modifying code.

#### Acceptance Criteria

1. WHEN the Execution_Module is initialized, THE Execution_Module SHALL read its runtime configuration from a `config.yaml` file located in the `execution/` directory.
2. THE Config_File SHALL contain the keys `fill_timeout_seconds` (positive integer or float, range [5, 300] — how long to wait for fill confirmation; booleans are rejected) and `poll_interval_seconds` (positive number int or float, range [0.5, 30] — how frequently to poll order status; booleans are rejected).
3. IF the Config_File is absent, THEN THE Execution_Module SHALL raise a `FileNotFoundError` with a message identifying the expected file path.
4. IF the Config_File contains invalid YAML syntax, THEN THE Execution_Module SHALL raise a `ValueError` with a message describing the syntax error.
5. IF the Config_File is valid YAML but parses to a non-dictionary type, THEN THE Execution_Module SHALL raise a `ValueError` stating that the configuration must be a YAML mapping.
6. IF the Config_File is valid YAML mapping but is missing any of the required keys, THEN THE Execution_Module SHALL raise a `ValueError` identifying each missing key.
7. IF any configuration value fails its type or range constraint (e.g., `fill_timeout_seconds` is not in range [5, 300], booleans are rejected for numeric fields), THEN THE Execution_Module SHALL raise a `ValueError` identifying the invalid key and stating the accepted constraint.
8. IF the Config_File contains keys beyond the required set, THE Execution_Module SHALL log a WARNING-level message identifying the unknown keys (to help catch typos), but SHALL silently ignore them functionally without raising an error.

---

### Requirement 9: Credential Loading

**User Story:** As a developer, I want the Execution Module to load Alpaca API credentials from the existing shared .env file, so that credentials are managed in one place and are never hard-coded.

#### Acceptance Criteria

1. THE Execution_Module SHALL actively load Alpaca API credentials by calling the existing `data_ingestion.credentials.load_credentials()` function during initialization, reusing the same credential-loading logic used by the data ingestion module, and SHALL use the returned credentials to instantiate the Alpaca_Paper_Client.
2. THE Execution_Module SHALL NOT define its own credential-loading logic or read directly from the `.env` file — credential management is delegated to `data_ingestion.credentials`.
3. THE Execution_Module SHALL NOT log, print, or include credential values (the actual API key or secret key strings) in any exception message, log event, or return value — only credential variable names (e.g., `"APCA_API_KEY_ID"`) may appear in output.
4. IF `load_credentials()` raises an `EnvironmentError` (credentials missing or empty), THEN THE Execution_Module SHALL propagate the exception to the caller without modification.

---

### Requirement 10: Module File Structure

**User Story:** As a developer, I want the Execution Module to follow the same file structure as the risk_engine and strategy modules, so that the project remains consistent and navigable.

#### Acceptance Criteria

1. THE Execution_Module SHALL be organized in a directory named `execution/` at the project root, containing the files: `__init__.py`, `config_loader.py`, `validators.py`, `execute.py`, and `config.yaml`.
2. THE `config_loader.py` file SHALL contain an `ExecutionConfig` dataclass (using `@dataclass` from `dataclasses`) with typed fields for each config key, and a `load_config(config_path: str) -> ExecutionConfig` function that validates all parameters and returns the dataclass instance.
3. THE `validators.py` file SHALL contain a `validate_decision_dict(decision_dict)` function that performs all input validation checks defined in Requirement 1 and raises `TypeError` or `ValueError` on invalid input.
4. THE `execute.py` file SHALL contain the public `execute_decision()` function as the module's primary entry point for order execution.
5. THE `__init__.py` file SHALL re-export `ExecutionConfig`, `load_config`, and `execute_decision` as the module's public API surface via an `__all__` list.
6. THE `config.yaml` file SHALL contain default configuration values with explanatory comments, following the same style as `risk_engine/config.yaml`.

---

### Requirement 11: Order Result Structure

**User Story:** As a developer, I want a structured return value from the Execution Module, so that the pipeline and logging can inspect the outcome of every execution attempt.

#### Acceptance Criteria

1. WHEN `execute_decision` completes (whether an order was placed or the decision was skipped), THE Execution_Module SHALL return an Order_Result dictionary containing: `status` (one of `"filled"`, `"partial_fill"`, `"cancelled"`, `"expired"`, `"rejected"`, `"timeout"`, `"skipped"`, `"error"`), `decision` (the original decision string), `signal` (the original signal string), `symbol` (the ticker from the Decision_Dict, or `None` if not present), `position_size` (the requested quantity from the Decision_Dict as a non-negative integer), `reason` (a non-empty string of 1–500 characters explaining the result), and `timestamp` (the original Decision_Dict timestamp as a UTC-aware pandas Timestamp).
2. WHEN the order is filled or partially filled, THE Order_Result SHALL additionally contain: `entry_order_id` (Alpaca order ID string), `stop_loss_order_id` (Alpaca stop-loss order ID string or `None` if the stop-loss leg was not confirmed), `fill_price` (positive float greater than 0.0), `filled_quantity` (int ≥ 1 representing actual shares filled), and `filled_at` (UTC-aware pandas Timestamp representing when the fill was confirmed). THE `position_size` in the Order_Result SHALL be ≥ 1 (matching the validated input) whenever `status` is `"filled"` or `"partial_fill"`.
3. WHEN an API error prevents order submission, THE Execution_Module SHALL return an Order_Result with `status` set to `"error"`, `reason` containing the error description (truncated to 500 characters if necessary), and `error_type` containing the exception class name as a string.
4. THE Execution_Module SHALL ensure `status` is always exactly one of the eight defined values (`"filled"`, `"partial_fill"`, `"cancelled"`, `"expired"`, `"rejected"`, `"timeout"`, `"skipped"`, `"error"`) — no other value is permitted.
5. WHEN the status is `"partial_fill"`, THE Order_Result SHALL additionally contain `cancelled_quantity` (int ≥ 1 representing shares that were cancelled after timeout).
6. WHEN the status is `"cancelled"`, `"expired"`, `"rejected"`, or `"timeout"` (terminal states where the order was submitted but not filled), THE Order_Result SHALL additionally contain `entry_order_id` (Alpaca order ID string) to enable end-to-end tracing of the submitted order.

---

### Requirement 12: Error Handling, Resilience, and State Recovery

**User Story:** As a developer, I want the Execution Module to handle API errors gracefully and always query actual order state from Alpaca before taking action after a failure, so that the module never acts on stale local assumptions.

#### Acceptance Criteria

1. IF the Alpaca API returns an HTTP error (4xx or 5xx) during order submission, THEN THE Execution_Module SHALL catch the exception, log the error at ERROR level as a structured event, and return an Order_Result with `status` set to `"error"` and `reason` containing the HTTP status code and error message. THE `entry_order_id` SHALL be set to `None` since order submission did not succeed.
2. IF a network timeout or connection error occurs during API communication, THEN THE Execution_Module SHALL catch the exception, log the error at ERROR level as a structured event, and return an Order_Result with `status` set to `"error"` and `reason` describing the connectivity failure including the exception class name.
3. IF a connection is dropped or a network error occurs during status polling (after the order has been submitted), THEN THE Execution_Module SHALL attempt to re-query the Alpaca API for the actual current state of the order by its order ID exactly once; IF the re-query succeeds, THE Execution_Module SHALL use the returned order state to determine the Order_Result; IF the re-query also fails, THEN THE Execution_Module SHALL return an Order_Result with `status` set to `"error"`, `reason` describing both the original polling failure and the re-query failure, and `entry_order_id` set to the Alpaca order ID of the submitted order.
4. IF an unexpected exception occurs during execution after input validation has passed, THEN THE Execution_Module SHALL catch the exception, log it at ERROR level as a structured event, and return an Order_Result with `status` set to `"error"` rather than propagating the exception and crashing the pipeline.
5. THE Execution_Module SHALL NOT retry failed API calls automatically — all retry logic is the responsibility of the pipeline orchestrator, regardless of whether the failure is transient (network glitch) or permanent (authentication error). The module reports the failure and moves on.
6. WHEN recovering from an error during polling and the re-query to Alpaca succeeds, THE Execution_Module SHALL use the Alpaca order ID to query the specific order's current state, and SHALL set the Order_Result fields (`status`, `fill_price`, `filled_quantity`, `filled_at`) based on the re-queried state rather than the last locally observed status.
7. IF an error occurs after order submission (during polling or fill confirmation), THEN THE Execution_Module SHALL include the `entry_order_id` in the returned Order_Result so that the pipeline orchestrator or manual intervention can trace and reconcile the order. IF the error occurs before or during submission (before Alpaca assigns an order ID), THEN `entry_order_id` SHALL be omitted or set to `None`.
