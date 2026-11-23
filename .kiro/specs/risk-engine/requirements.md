# Requirements Document

## Introduction

The `risk_engine` module is the third stage in the Minions paper-trading pipeline. It receives a signal dictionary from the `strategy` module and a snapshot of the current account state (available cash, list of open positions with their sizes, and today's realized P&L) as function parameters — it does not fetch this data itself. It applies four guardrails: (1) caps any single trade's position size at a configurable percentage of account equity, (2) rejects all new trades if today's realized loss exceeds a configurable daily loss limit, (3) rejects new trades if the number of currently open positions is at or above a configurable maximum, (4) attaches a mandatory stop-loss price to every approved trade. The module returns either an approved order with calculated size and stop-loss, or a rejection with a reason. This module has NO access to the Alpaca API and cannot place, modify, or cancel orders itself — it only ever returns a decision for the downstream execution module to act on.

---

## Glossary

- **Risk_Engine**: The `risk_engine/` Python module described in this document.
- **Signal_Dict**: The dictionary produced by the `strategy` module with keys `signal` (one of `"BUY"`, `"SELL"`, `"HOLD"`), `short_sma` (float or None), `long_sma` (float or None), `timestamp` (UTC-aware pandas Timestamp), and optionally `symbol` (string, 1–10 characters).
- **Account_Snapshot**: A dictionary passed to the Risk Engine by the caller containing `equity` (positive float, total account value in USD), `cash` (non-negative float, available cash in USD), `open_positions` (a list of dictionaries, each with `symbol` (string) and `size` (positive integer representing shares held)), and `daily_realized_pnl` (float, today's realized profit-and-loss in USD — negative means a loss).
- **Decision**: The output classification of the Risk Engine for a given Signal_Dict — one of `"APPROVED"`, `"VETOED"`, or `"NO_ACTION"`.
- **Decision_Dict**: The structured dictionary returned by the Risk Engine containing the Decision, the original signal, sizing information, stop-loss price, and the reason for the decision. `"APPROVED"` means a real trade is approved for execution; `"VETOED"` means the trade was rejected; `"NO_ACTION"` means no trade is needed (used exclusively for HOLD signals).
- **Position_Size**: The number of shares (integer, ≥ 1) to buy or sell in a single trade.
- **Max_Position_Percent**: The configurable maximum percentage of account equity that a single trade may represent. Used to compute the maximum allowed Position_Size.
- **Max_Daily_Loss**: The configurable maximum realized loss (in USD, as a positive number) allowed in a single trading day before all further BUY/SELL signals are vetoed.
- **Max_Open_Positions**: The configurable maximum number of concurrent open positions allowed before new BUY signals are vetoed.
- **Stop_Loss_Percent**: The configurable percentage (0 < percent < 100) used to compute the stop-loss price from the entry price.
- **Stop_Loss_Price**: The price level at which a position must be exited to limit loss. Computed as `entry_price * (1 - stop_loss_percent / 100)` for BUY trades, or `entry_price * (1 + stop_loss_percent / 100)` for SELL trades.
- **Entry_Price**: The current market price at which a trade would be entered, passed to the Risk Engine as the `current_price` parameter.
- **Config_File**: The `config.yaml` file located in the `risk_engine/` directory containing configurable risk parameters.
- **Pipeline**: The ordered sequence Data Ingestion → Strategy Engine → Risk Engine → Order Execution.

---

## Requirements

### Requirement 1: Signal Input Validation

**User Story:** As a developer, I want the Risk Engine to validate incoming signals and account state before processing, so that malformed or unexpected data never triggers a risk decision.

#### Acceptance Criteria

1. WHEN `evaluate_signal(signal_dict, current_price, account_snapshot)` is called, THE Risk_Engine SHALL validate parameters in the following strict order: first `signal_dict` (criteria 2–5), then `current_price` (criterion 6), then `account_snapshot` (criterion 7); IF multiple parameters are invalid, THEN THE Risk_Engine SHALL raise the exception for the first invalid parameter in this order.
2. WHEN `evaluate_signal` is called, THE Risk_Engine SHALL verify that `signal_dict` is a Python dictionary; IF `signal_dict` is not a dictionary (including `None`), THEN THE Risk_Engine SHALL raise a `TypeError` whose message states the actual type received.
3. WHEN `signal_dict` is a dictionary, THE Risk_Engine SHALL verify that it contains the key `signal` with a value that is one of `"BUY"`, `"SELL"`, or `"HOLD"`; IF the key is missing or the value is not one of those three strings, THEN THE Risk_Engine SHALL raise a `ValueError` whose message identifies the invalid or missing key.
4. WHEN `signal_dict` is a dictionary, THE Risk_Engine SHALL verify that it contains the key `timestamp` with a value that is a timezone-aware pandas Timestamp; IF the key is missing or the value is not a timezone-aware pandas Timestamp, THEN THE Risk_Engine SHALL raise a `ValueError` whose message identifies the invalid or missing key.
5. WHEN `signal_dict` is a dictionary and contains an optional `symbol` key, THE Risk_Engine SHALL verify that its value is a string between 1 and 10 characters (inclusive); IF the value is present but is not a string of 1–10 characters, THEN THE Risk_Engine SHALL raise a `ValueError` whose message identifies the invalid `symbol` value.
6. WHEN `evaluate_signal` is called, THE Risk_Engine SHALL verify that `current_price` is a positive number (int or float, greater than zero); IF `current_price` is `None`, not numeric, or not greater than zero, THEN THE Risk_Engine SHALL raise a `ValueError` whose message states that `current_price` must be a positive number.
7. WHEN `evaluate_signal` is called, THE Risk_Engine SHALL verify that `account_snapshot` is a dictionary (not `None`) containing the keys `equity` (positive float), `cash` (non-negative float), `open_positions` (a list of dictionaries each with `symbol` (non-empty string) and `size` (positive integer)), and `daily_realized_pnl` (float); IF `account_snapshot` is `None` or not a dictionary, THEN THE Risk_Engine SHALL raise a `TypeError` whose message states the actual type received; IF it is a dictionary but any key is missing or any value fails its type/range constraint, THEN THE Risk_Engine SHALL raise a `ValueError` whose message identifies the first invalid or missing key.

---

### Requirement 2: HOLD Signal Pass-Through

**User Story:** As a developer, I want HOLD signals to pass through the Risk Engine without any risk checks, so that only actionable signals consume risk evaluation resources.

#### Acceptance Criteria

1. WHEN `signal_dict` contains `signal` equal to `"HOLD"`, THE Risk_Engine SHALL return a Decision_Dict with `decision` set to `"NO_ACTION"`, `signal` set to `"HOLD"`, `position_size` set to `0`, `stop_loss` set to `None`, `reason` set to `"HOLD signals require no risk evaluation"`, and `timestamp` set to the original `signal_dict["timestamp"]` value. IF `signal_dict` contains a `symbol` key, THEN the Decision_Dict SHALL also include `symbol` with the same value.
2. WHEN `signal_dict` contains `signal` equal to `"HOLD"`, THE Risk_Engine SHALL NOT check any risk limits (max daily loss, max open positions, max position size, or stop-loss computation).

---

### Requirement 3: Maximum Position Size Enforcement

**User Story:** As a developer, I want the Risk Engine to cap the dollar value of any single trade at a configurable percentage of account equity, so that no single trade can expose the account to excessive risk.

#### Acceptance Criteria

1. THE Risk_Engine SHALL read `max_position_percent` from the Config_File as a float in the range (0, 100) exclusive, representing the maximum percentage of account equity that a single trade may represent.
2. WHEN `evaluate_signal` is called with a BUY or SELL signal that passes all prior risk checks, THE Risk_Engine SHALL compute the maximum allowed capital for the trade as `account_snapshot["equity"] * (max_position_percent / 100)`.
3. WHEN `evaluate_signal` is called with a BUY signal that passes all prior risk checks, THE Risk_Engine SHALL compute the Position_Size as `min(floor(max_allowed_capital / current_price), floor(account_snapshot["cash"] / current_price))`, ensuring the trade does not exceed either the equity-percent limit or the available cash.
4. WHEN `evaluate_signal` is called with a SELL signal that passes all prior risk checks, THE Risk_Engine SHALL set the Position_Size equal to the `size` value of the position in `account_snapshot["open_positions"]` whose `symbol` matches `signal_dict["symbol"]`, closing the full held position. The equity-percent cap does not apply to SELL signals because selling reduces risk rather than adding it.
5. IF the computed Position_Size for a BUY signal is zero because `current_price` exceeds the maximum allowed capital, THEN THE Risk_Engine SHALL veto the trade with a `reason` stating that the equity-percent limit prevents a valid position size. IF the computed Position_Size for a BUY signal is zero because `current_price` exceeds the available cash but not the equity-percent limit, THEN THE Risk_Engine SHALL veto the trade with a `reason` stating that insufficient cash prevents a valid position size. IF both constraints independently produce zero, THEN THE Risk_Engine SHALL report the equity-percent limit as the binding constraint.
6. IF the computed Position_Size is greater than zero AND all subsequent checks pass, THE Risk_Engine SHALL approve the trade at that computed size.

---

### Requirement 4: Maximum Daily Loss Enforcement

**User Story:** As a developer, I want the Risk Engine to veto all new trades once the daily loss limit is reached, so that a bad trading day cannot wipe out the account.

#### Acceptance Criteria

1. THE Risk_Engine SHALL read `max_daily_loss` from the Config_File as a positive float representing the maximum realized loss in USD allowed in a single trading day.
2. WHEN `evaluate_signal` is called with a BUY or SELL signal AND `account_snapshot["daily_realized_pnl"]` is less than or equal to the negative of `max_daily_loss` (meaning realized losses have reached or exceeded the limit), THEN THE Risk_Engine SHALL return a Decision_Dict with `decision` set to `"VETOED"`, `position_size` set to `0`, `stop_loss` set to `None`, and `reason` containing a message that includes the numeric value of `daily_realized_pnl` and the numeric value of `max_daily_loss`.
3. IF `account_snapshot["daily_realized_pnl"]` is greater than the negative of `max_daily_loss` (losses have not yet reached the limit), THEN THE Risk_Engine SHALL not veto the signal for daily loss and SHALL allow evaluation to continue to the next risk check in the defined order.

---

### Requirement 5: Maximum Open Positions Enforcement

**User Story:** As a developer, I want the Risk Engine to veto new BUY signals when the maximum number of open positions is reached, so that portfolio concentration risk is bounded.

#### Acceptance Criteria

1. THE Risk_Engine SHALL read `max_open_positions` from the Config_File as a positive integer (1 ≤ value ≤ 1000) representing the maximum number of concurrent open positions allowed.
2. WHEN `evaluate_signal` is called with a BUY signal AND the length of `account_snapshot["open_positions"]` is greater than or equal to `max_open_positions`, THEN THE Risk_Engine SHALL return a Decision_Dict with `decision` set to `"VETOED"`, `position_size` set to `0`, `stop_loss` set to `None`, and `reason` stating that the maximum open positions limit has been reached (including the current count and the limit).
3. WHEN `evaluate_signal` is called with a SELL signal, THE Risk_Engine SHALL NOT apply the max open positions check (selling reduces exposure and should not be blocked by this rule).
4. WHEN `evaluate_signal` is called with a SELL signal AND `signal_dict` contains a `symbol` key, THE Risk_Engine SHALL verify that the symbol appears in `account_snapshot["open_positions"]` with `size` greater than zero; IF the symbol is not found in the open positions list, THEN THE Risk_Engine SHALL return a Decision_Dict with `decision` set to `"VETOED"`, `position_size` set to `0`, `stop_loss` set to `None`, and `reason` stating that the position is not currently held and cannot be sold.
5. WHEN `evaluate_signal` is called with a SELL signal AND `signal_dict` does NOT contain a `symbol` key, THE Risk_Engine SHALL return a Decision_Dict with `decision` set to `"VETOED"`, `position_size` set to `0`, `stop_loss` set to `None`, and `reason` stating that a symbol is required for SELL signals to verify the position is held.
6. WHEN the length of `account_snapshot["open_positions"]` is less than `max_open_positions`, THE Risk_Engine SHALL allow the BUY signal to proceed to further risk checks.

---

### Requirement 6: Mandatory Stop-Loss Computation

**User Story:** As a developer, I want every approved trade to include a stop-loss price, so that no position can be held without a predefined exit point for loss limitation.

#### Acceptance Criteria

1. THE Risk_Engine SHALL read `stop_loss_percent` from the Config_File as a float in the range (0, 100) exclusive, representing the percentage distance from the entry price at which the stop-loss is placed.
2. WHEN a BUY signal passes all prior risk checks (daily loss limit, max open positions, and position sizing), THE Risk_Engine SHALL compute `stop_loss` as `round(current_price * (1 - stop_loss_percent / 100), 2)`.
3. WHEN a SELL signal passes all prior risk checks (daily loss limit, position-held verification, and position sizing), THE Risk_Engine SHALL compute `stop_loss` as `round(current_price * (1 + stop_loss_percent / 100), 2)`.
4. THE Risk_Engine SHALL include the computed `stop_loss` value in every Decision_Dict where `decision` is `"APPROVED"`.
5. IF the computed `stop_loss` for a BUY signal is less than or equal to zero (possible with very low-priced assets and high stop-loss percentages), THEN THE Risk_Engine SHALL return a Decision_Dict with `decision` set to `"VETOED"`, `position_size` set to `0`, `stop_loss` set to `None`, and `reason` stating that a valid stop-loss price cannot be computed.
6. WHEN a SELL signal passes all prior risk checks, THE Risk_Engine SHALL approve the trade without a stop-loss validity veto, since `stop_loss_percent` being in range (0, 100) guarantees the computed stop-loss is always greater than `current_price`.

---

### Requirement 7: Configuration

**User Story:** As a developer, I want all risk parameters to be configurable via a YAML file, so that I can tune risk limits without modifying code.

#### Acceptance Criteria

1. WHEN the Risk_Engine is initialized, THE Risk_Engine SHALL read its runtime configuration from a `config.yaml` file located in the `risk_engine/` directory.
2. THE Config_File SHALL contain the keys `max_position_percent` (float, 0 < value < 100), `max_daily_loss` (positive float, in USD), `max_open_positions` (positive integer, 1 ≤ value ≤ 1000), and `stop_loss_percent` (float, 0 < value < 100).
3. IF the Config_File is absent, THEN THE Risk_Engine SHALL raise a `FileNotFoundError` with a message identifying the expected file path.
4. IF the Config_File contains invalid YAML syntax, THEN THE Risk_Engine SHALL raise a `ValueError` with a message describing the syntax error.
5. IF the Config_File is valid YAML but parses to a non-dictionary type (e.g., a list, string, or null), THEN THE Risk_Engine SHALL raise a `ValueError` stating that the configuration must be a YAML mapping.
6. IF the Config_File is valid YAML mapping but is missing any of the required keys, THEN THE Risk_Engine SHALL raise a `ValueError` identifying each missing key.
7. IF any configuration value fails its type or range constraint (e.g., `max_position_percent` is not in range (0, 100), `max_open_positions` is not a positive integer, booleans are rejected for numeric fields), THEN THE Risk_Engine SHALL raise a `ValueError` identifying the invalid key and stating the accepted constraint.

---

### Requirement 8: Output Structure

**User Story:** As a developer, I want the Risk Engine to return a structured decision dictionary, so that the execution module and logging can inspect both the decision and the reasoning.

#### Acceptance Criteria

1. WHEN `evaluate_signal` completes successfully, THE Risk_Engine SHALL return a plain Python `dict` (Decision_Dict) containing exactly the following keys and no others (aside from the conditional `symbol` key defined in criterion 2): `decision` (one of `"APPROVED"`, `"VETOED"`, `"NO_ACTION"`), `signal` (the original signal string from Signal_Dict), `position_size` (non-negative integer — the final number of shares, 0 for NO_ACTION or vetoed signals), `stop_loss` (a Python `float` rounded to 2 decimal places for approved signals, or `None` for NO_ACTION/vetoed signals), `reason` (a non-empty string of 1–500 characters explaining the decision), and `timestamp` (the original UTC-aware timestamp from Signal_Dict, passed through unmodified).
2. WHEN the original Signal_Dict contains a `symbol` key, THE Risk_Engine SHALL include `symbol` in the Decision_Dict with the same value.
3. IF the original Signal_Dict does not contain a `symbol` key, THEN THE Risk_Engine SHALL omit `symbol` from the Decision_Dict.
4. THE Risk_Engine SHALL ensure `decision` is always exactly one of `"APPROVED"`, `"VETOED"`, or `"NO_ACTION"` — no other value is permitted. `"APPROVED"` indicates a real trade is cleared for execution; `"VETOED"` indicates the trade was rejected; `"NO_ACTION"` indicates no trade is needed (used exclusively for HOLD signals).
5. THE Risk_Engine SHALL ensure `position_size` is always a non-negative integer (0 for vetoed or NO_ACTION signals, ≥ 1 for approved BUY/SELL signals).
6. IF an unexpected error occurs during risk evaluation after input validation has passed, THEN THE Risk_Engine SHALL propagate the exception to the caller rather than returning a malformed Decision_Dict — the caller is responsible for handling unexpected failures.

---

### Requirement 9: Risk Check Ordering

**User Story:** As a developer, I want risk checks to execute in a defined priority order, so that the most critical limits (daily loss) are evaluated first and the reason for any veto is unambiguous.

#### Acceptance Criteria

1. WHEN `evaluate_signal` is called with a BUY or SELL signal that has passed input validation (Requirement 1) and is not a HOLD signal (Requirement 2), THE Risk_Engine SHALL evaluate risk checks in the following strict order: (1) daily loss limit, (2) max open positions (BUY only) or position-held verification (SELL only), (3) position sizing with equity-percent cap (BUY only — SELL signals use the full held position size determined in step 2), (4) stop-loss computation and validation.
2. WHEN a risk check at step N in the defined order results in a veto, THE Risk_Engine SHALL return a Decision_Dict with `decision` set to `"VETOED"` and `reason` identifying the failing check — subsequent checks (steps N+1 through 4) SHALL NOT be evaluated.
3. WHEN multiple risk checks would each independently cause a veto, THE Risk_Engine SHALL report only the first one encountered in the defined order as the `reason`, verifiable by confirming the `reason` field references the check corresponding to the lowest step number that would fail.

---

### Requirement 10: No Order Placement Enforcement

**User Story:** As a developer, I want a hard guarantee that the Risk Engine never places, modifies, or cancels orders, so that a bug in this module cannot cause unintended trades.

#### Acceptance Criteria

1. THE Risk_Engine SHALL NOT import or instantiate any class from the `alpaca.trading`, `alpaca.broker`, or `alpaca.data` packages, nor from any third-party or standard-library module that provides network access (including but not limited to `socket`, `urllib`, `http.client`, `requests`, `httpx`, `websocket`, and `aiohttp`).
2. THE Risk_Engine SHALL NOT import or instantiate any class that exposes methods for placing, modifying, or cancelling orders — specifically, the symbols `TradingClient`, `BrokerClient`, `trading_client`, and `broker_client` SHALL NOT appear in any import statement within the module's source files.
3. THE Risk_Engine SHALL NOT import or invoke `subprocess`, `os.system`, `os.popen`, `os.exec` variants, `os.spawn` variants, or `shutil.which` with subsequent execution — nor any other standard-library facility that spawns external processes (specifically including `multiprocessing.Process` when used to invoke external commands).
4. WHILE `evaluate_signal` is executing, THE Risk_Engine SHALL accept input exclusively through its public function parameters — it SHALL NOT read from files, databases, or network sockets. Reading the Config_File during module initialization (prior to `evaluate_signal` invocation) is the sole permitted file-read operation.
5. WHILE `evaluate_signal` is executing, THE Risk_Engine SHALL produce output exclusively through its return value — it SHALL NOT write to files, databases, or network sockets. Standard-library logging (the `logging` module) is exempt from this prohibition as it constitutes observability, not functional output.
6. WHEN a static AST scan is performed on all `.py` source files within the `risk_engine/` directory, THE scan SHALL confirm that no import statement references any symbol or module path listed as forbidden in criteria 1, 2, and 3 above.
7. THE Risk_Engine SHALL NOT use dynamic import mechanisms — specifically `importlib.import_module`, `__import__()`, `eval()`, or `exec()` — anywhere in its source files; the static AST scan defined in criterion 6 SHALL additionally flag the presence of any of these four constructs as a violation.

---

### Requirement 11: Determinism

**User Story:** As a developer, I want the Risk Engine to be fully deterministic given the same inputs, so that I can replay scenarios for debugging and verify behavior in tests.

#### Acceptance Criteria

1. FOR ALL valid inputs (same `signal_dict`, same `current_price`, same `account_snapshot`, same configuration), calling `evaluate_signal` SHALL always produce the same Decision_Dict — exact match for `decision`, `signal`, `position_size`, `reason`, `timestamp`, and `symbol`; exact match for `stop_loss` (since values are rounded to 2 decimal places per Requirement 6, no floating-point tolerance is needed beyond the rounding).
2. THE Risk_Engine SHALL NOT use random number generation, system time (except for the timestamp passed in via Signal_Dict), thread-dependent ordering, global mutable state, or any other source of non-determinism during signal evaluation.
3. THE Risk_Engine SHALL be stateless across invocations — calling `evaluate_signal` SHALL NOT modify any module-level or instance-level state that could influence the result of a subsequent `evaluate_signal` call. All state (daily P&L, open positions, equity, cash) is passed in via the `account_snapshot` parameter by the caller.
4. THE Risk_Engine SHALL NOT mutate any of the input arguments (`signal_dict`, `account_snapshot`) during evaluation — callers may reuse these objects after the call returns.
