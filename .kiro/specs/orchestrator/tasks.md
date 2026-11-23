# Implementation Plan: Orchestrator Module

## Overview

Implement the `orchestrator/` module as the fifth and final module in the Minions paper-trading pipeline. The Orchestrator ties together the four existing modules (Data Ingestion → Strategy Engine → Risk Engine → Execution) into a continuous, unattended loop. It anchors its schedule to new daily bar availability, queries Alpaca for fresh account state every cycle, maintains a durable state file for position records and scheduling state, handles errors with a severity split (pre-order → retry, post-order → halt), supports graceful shutdown via `threading.Event` for interruptible waits, and performs a startup safety check for unprotected positions. Implementation uses Python with explicit, well-commented code. Property-based testing uses Hypothesis.

## Tasks

- [ ] 1. Set up module structure, configuration, and state management
  - [ ] 1.1 Create `orchestrator/` directory with `__init__.py`, `config.yaml`, and `config_loader.py`
    - Create `orchestrator/` directory at the project root
    - Create `orchestrator/__init__.py` with module-level docstring forbidding live trading, re-exporting `OrchestratorConfig`, `load_config`, and `run_loop` via `__all__`
    - Create `orchestrator/config.yaml` with `bar_check_interval_seconds: 300` and explanatory comments for each parameter (symbol, state_file_path shown commented with their defaults)
    - Create `orchestrator/config_loader.py` with `OrchestratorConfig` dataclass (fields: `bar_check_interval_seconds: float`, `symbol: str`, `state_file_path: str`) and `load_config(config_path: str) -> OrchestratorConfig` following the 8-step algorithm: file existence → YAML parsing → non-dict check → required key presence → value type/range validation (booleans rejected for numeric field, bar_check_interval_seconds in [60, 3600], symbol 1–10 chars, state_file_path 1–255 chars) → warn on unknown keys → apply defaults for optional keys → return OrchestratorConfig
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 12.1, 12.2, 12.7_

  - [ ] 1.2 Create `orchestrator/state_manager.py` with state file I/O and position record logic
    - Implement `PositionRecord` dataclass with fields: symbol, entry_order_id, stop_loss_order_id, quantity, entry_price, entry_timestamp, status, exit_timestamp
    - Implement `StateData` dataclass with fields: positions (list), last_acted_bar (Optional[str]), pending_decision (Optional[dict])
    - Implement `load_state(state_file_path)`: if file missing create empty StateData, if invalid JSON raise ValueError
    - Implement `save_state(state_data, state_file_path)`: atomic write via tempfile + os.replace in same directory
    - Implement `add_position_record(state_data, order_result, symbol)`: append new open PositionRecord from filled/partial_fill BUY, return new StateData (immutable pattern)
    - Implement `update_position_on_sell(state_data, order_result, symbol)`: update matching open record on SELL fill — set closed/partially_closed, decrement quantity as needed
    - Implement `format_account_snapshot(account, positions)`: transform Alpaca objects to Risk Engine format, raise ValueError for equity ≤ 0 or cash < 0, truncate fractional qty to int, exclude positions with size ≤ 0
    - _Requirements: 1.2, 1.5, 1.6, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9_

  - [ ]* 1.3 Write property test for config validation (Property 3)
    - **Property 3: Config validation accepts valid values and rejects invalid ones**
    - **Validates: Requirements 8.2, 8.6, 8.7**
    - Test in `tests/test_orchestrator_properties.py`
    - Use Hypothesis strategies generating: valid configs (bar_check_interval_seconds in [60, 3600], symbol 1–10 chars, state_file_path 1–255 chars) → verify OrchestratorConfig returned; invalid configs (missing required key, out-of-range, wrong type, boolean for numeric) → verify ValueError raised with key name in message

  - [ ]* 1.4 Write property test for account snapshot formatting (Property 1)
    - **Property 1: Account snapshot formatting preserves data and conforms to schema**
    - **Validates: Requirements 1.2, 1.5**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate mock Alpaca TradeAccount (positive equity, non-negative cash, varying last_equity) and Position lists (0–10 positions with fractional quantities), verify output dict has correct equity/cash/daily_realized_pnl values and open_positions contains only entries with truncated qty ≥ 1. Also generate accounts with zero/negative equity or negative cash, verify ValueError raised.

  - [ ]* 1.5 Write property test for state file round-trip (Property 4)
    - **Property 4: State file round-trip serialization**
    - **Validates: Requirements 2.12, 6.7**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate valid StateData instances (varying position counts, last_acted_bar as ISO string or None, pending_decision as dict or None), serialize via save_state then deserialize via load_state, verify all fields are preserved

  - [ ]* 1.6 Write unit tests for config and state manager edge cases
    - Test in `tests/test_orchestrator_unit.py`
    - `test_config_file_not_found` — FileNotFoundError with path in message
    - `test_config_invalid_yaml` — ValueError on malformed YAML
    - `test_config_non_dict_yaml` — ValueError on YAML list
    - `test_config_missing_required_key` — ValueError naming missing key
    - `test_config_unknown_keys_warning` — WARNING logged, no error
    - `test_config_defaults_applied` — symbol and state_file_path use defaults when absent
    - `test_state_file_created_on_first_run` — empty state file created
    - `test_state_file_invalid_json_raises` — ValueError on corrupt file
    - `test_atomic_write_uses_rename` — verify temp-then-rename pattern (mock os.replace)
    - `test_pending_decision_persistence` — Decision_Dict survives write/read cycle
    - _Requirements: 6.5, 6.6, 8.3, 8.4, 8.5, 8.6, 8.8_

- [ ] 2. Implement safety check and paper-trading enforcement
  - [ ] 2.1 Create `orchestrator/safety_check.py` with `run_safety_check(client)`
    - Query all open positions via `client.get_all_positions()`
    - Query all open orders via `client.get_orders(filter=GetOrdersRequest(status="open"))`
    - For each position: determine long/short, search orders for matching stop-loss (type in stop/stop_limit, same symbol, correct side), log CRITICAL for each unprotected position with symbol/quantity/market_value
    - Log INFO summary: total positions checked, unprotected count
    - Return dict with `total_positions` and `unprotected_count`
    - Let exceptions propagate (caller handles by halting startup)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ]* 2.2 Write property test for safety check (Property 10)
    - **Property 10: Safety check correctly identifies unprotected positions**
    - **Validates: Requirements 7.2, 7.3**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate sets of mock positions (long/short) and mock stop/stop_limit orders with varying symbol/side combinations, verify the safety check identifies exactly the set of positions without a matching protective order

  - [ ]* 2.3 Write unit tests for safety check scenarios
    - Test in `tests/test_orchestrator_unit.py`
    - `test_all_positions_protected` — no CRITICAL logs emitted
    - `test_unprotected_position_logged` — CRITICAL for each unprotected position
    - `test_safety_check_api_failure_propagates` — exception propagates to caller
    - `test_pre_existing_positions_included` — positions without state file records still checked
    - _Requirements: 7.3, 7.5, 7.7_

- [ ] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Implement new-bar detection, market-open check, and pipeline cycle execution
  - [ ] 4.1 Implement `_check_new_bar()` and `_create_paper_client()` in `orchestrator/loop.py`
    - `_create_paper_client(api_key, secret_key)`: instantiate `TradingClient(api_key, secret_key, paper=True)` with hard-coded literal True (inline comment: "NEVER change this — paper trading only"), verify base URL contains "paper" (case-insensitive) → raise RuntimeError if not
    - `_check_new_bar(rest_client, symbol, last_acted_bar)`: fetch latest daily bar via RestClient, compare date portion (YYYY-MM-DD UTC) against last_acted_bar, return (is_new, bar_timestamp). If last_acted_bar is None, always return True.
    - Add imports: logging, signal, sys, time, threading, datetime, pandas, alpaca TradingClient, all four pipeline modules, orchestrator submodules
    - Create logger named `"orchestrator"`
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 9.1, 9.2, 9.3, 9.4, 9.5, 11.1_

  - [ ] 4.2 Write AST-scan purity tests in `tests/test_orchestrator_purity.py`
    - `test_paper_true_ast_scan` — AST scan all `.py` files in `orchestrator/` verifying `paper=True` in every `TradingClient` instantiation, no `paper=False`, no variable used for `paper` parameter
    - `test_no_direct_env_reading` — AST scan verifying `orchestrator/` does not import `dotenv` or call `os.getenv` directly
    - `test_no_live_trading_config` — verify no config key for paper/live mode selection in orchestrator/config.yaml
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [ ]* 4.3 Write property test for new-bar detection (Property 2)
    - **Property 2: New bar detection is a strict date comparison**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.5**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate pairs of UTC date strings, verify _check_new_bar returns True iff latest_bar_date > last_acted_bar_date (date portion only). When last_acted_bar is None, always returns True.

  - [ ] 4.4 Implement `_run_cycle()` in `orchestrator/loop.py`
    - Execute pipeline stages in strict order: query account state → format_account_snapshot → Data Ingestion (RestClient.get_historical_bars) → Strategy (compute_signal) → Risk Engine (evaluate_signal with current_price from df["close"].iloc[-1]) → check for vetoed/no_action/hold → check market open → Execution (execute_decision) or defer
    - After each stage, check return type — if None or unexpected type, raise a contract violation error (classified as post_order / halt)
    - Include `symbol` from OrchestratorConfig in signal_dict passed to Risk Engine
    - Load strategy long_window from strategy config to compute start date for bar fetching (at least long_window + 1 trading days before current UTC date)
    - Return (outcome, order_result) tuple — outcome is one of: filled, partial_fill, vetoed, no_action, skipped, cancelled, expired, rejected, timeout, deferred
    - _Requirements: 1.1, 1.2, 1.3, 2.8, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [ ]* 4.5 Write property test for current price extraction (Property 11)
    - **Property 11: Current price extraction from DataFrame**
    - **Validates: Requirements 3.3**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate valid bar DataFrames (1–50 rows with OHLCV columns), verify current_price equals df["close"].iloc[-1]

  - [ ]* 4.6 Write unit tests for cycle execution paths
    - Test in `tests/test_orchestrator_unit.py`
    - `test_full_cycle_buy_filled` — end-to-end with all mocked modules, verify position record created
    - `test_full_cycle_sell_filled` — end-to-end sell cycle, verify position record updated
    - `test_pipeline_invocation_order` — verify strict Data Ingestion → Strategy → Risk → Execution ordering via mock call_args tracking
    - `test_vetoed_is_correct_decline` — no error log, cycle complete, Last_Acted_Bar updated
    - `test_skipped_is_correct_decline` — no error log, cycle complete
    - `test_market_closed_defers_execution` — decision persisted to state file, execution not called
    - `test_first_run_triggers_cycle` — no last_acted_bar → cycle runs immediately
    - `test_same_bar_no_cycle` — same bar date → idle
    - `test_stage_returns_none_halts` — contract violation halts loop (not retried)
    - _Requirements: 2.5, 2.8, 2.9, 3.1, 3.4, 3.5, 5.7_

- [ ] 5. Implement error classification and consecutive failure tracking
  - [ ] 5.1 Implement `_classify_error()` in `orchestrator/loop.py`
    - Errors in account_query, data_ingestion, strategy, risk_engine → "pre_order"
    - Execution returns Order_Result with status="error" and entry_order_id=None → "pre_order"
    - Execution returns Order_Result with status="error" and entry_order_id≠None → "post_order"
    - Raw exception from Execution (no Order_Result) → "post_order"
    - Any pipeline stage returning None/unexpected type (contract violation) → "post_order"
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.8_

  - [ ]* 5.2 Write property test for error classification (Property 5)
    - **Property 5: Error classification based on entry_order_id and contract violations**
    - **Validates: Requirements 3.2, 5.3, 5.4, 5.8**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate Order_Results with status="error" and varying entry_order_id (None vs present), verify classification. Generate raw exceptions from execution stage, verify "post_order". Generate contract violations (None return from stages), verify "post_order".

  - [ ]* 5.3 Write property test for correct declines (Property 6)
    - **Property 6: Correct declines never trigger halt or error log**
    - **Validates: Requirements 5.7**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate Risk Engine vetoes (decision="VETOED"), NO_ACTION outcomes, and Execution status="skipped" results, verify no ERROR/CRITICAL logging and no loop halt

  - [ ]* 5.4 Write property test for consecutive failure escalation (Property 7)
    - **Property 7: Consecutive failure escalation at threshold**
    - **Validates: Requirements 5.9**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate sequences of N consecutive pre-order failures (N from 1 to 10), verify WARNING for failures 1–2, ERROR for failures 3+. Verify counter resets to zero after a successful cycle.

  - [ ]* 5.5 Write unit tests for error handling
    - Test in `tests/test_orchestrator_unit.py`
    - `test_pre_order_failure_skips_and_continues` — cycle skipped, loop continues
    - `test_post_order_failure_halts` — CRITICAL logged, state flushed, sys.exit(1)
    - `test_raw_execution_exception_halts` — treated as post-order failure
    - `test_consecutive_failure_escalation` — ERROR at 3rd consecutive, resets on success
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.8, 5.9_

- [ ] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement main loop with graceful shutdown and deferred execution
  - [ ] 7.1 Implement `run_loop()` in `orchestrator/loop.py` — initialization phase
    - Load orchestrator config via load_config
    - Load credentials via data_ingestion.credentials.load_credentials() (let EnvironmentError propagate)
    - Create paper TradingClient via _create_paper_client (verify paper safety)
    - Create RestClient for data ingestion
    - Load strategy, risk, execution configs via their respective load_config functions
    - Load or create state file via load_state
    - Check for pending deferred decision (log WARNING if found, resume waiting)
    - Run safety check (halt startup on failure with CRITICAL log and non-zero exit)
    - Register signal handlers for SIGINT and SIGTERM
    - Initialize threading.Event for interruptible idle waits
    - _Requirements: 1.1, 4.4, 7.1, 7.7, 9.1, 9.2, 9.3, 9.5, 9.6, 11.1, 11.2, 11.3, 11.4_

  - [ ] 7.2 Implement `run_loop()` — main loop, shutdown, and deferred execution logic
    - Main loop: check stop_flag → handle deferred decision (recheck market open at bar_check_interval_seconds) → check new bar → run cycle → classify errors → update state → log events
    - Use threading.Event.wait(timeout=bar_check_interval_seconds) for interruptible idle (signal handler calls event.set() to wake immediately)
    - Graceful shutdown: on stop_flag set between cycles, flush state file atomically, log shutdown_completed, sys.exit(0). If holding deferred decision on shutdown, discard it, clear pending_decision from state file, log WARNING.
    - Duplicate signal handling: log WARNING with "duplicate_shutdown_signal", continue waiting
    - Cycle timeout warning: if cycle runs > 300s with pending shutdown, log WARNING with elapsed time
    - Deferred execution: when market closed, persist Decision_Dict to state file, recheck at bar_check_interval_seconds, execute when market opens, update Last_Acted_Bar only after resolution
    - On halt (post-order failure): flush state, log CRITICAL with entry_order_id/symbol, sys.exit(1)
    - Structured logging for cycle_started (INFO), cycle_completed (INFO), cycle_skipped (WARNING/ERROR based on consecutive count), loop_halted (CRITICAL), shutdown_initiated (INFO), new_bar_detected (DEBUG)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 3.8, 3.9, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.9, 6.9, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [ ]* 7.3 Write property test for position record creation on BUY fill (Property 8)
    - **Property 8: Position record creation on BUY fill**
    - **Validates: Requirements 6.2**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate valid filled/partial_fill Order_Results for BUY signals with varying symbol/order_id/quantity/price, verify PositionRecord has correct fields: symbol matches, entry_order_id matches, quantity equals filled_quantity, entry_price rounded to 2 decimals, status is "open"

  - [ ]* 7.4 Write property test for position record update on SELL (Property 9)
    - **Property 9: Position record update on SELL**
    - **Validates: Requirements 6.3**
    - Test in `tests/test_orchestrator_properties.py`
    - Generate existing open PositionRecords and SELL Order_Results (filled → closed with exit_timestamp; partial_fill → quantity decremented, partially_closed or closed if remaining is zero)

  - [ ]* 7.5 Write unit tests for main loop scenarios
    - Test in `tests/test_orchestrator_unit.py`
    - `test_shutdown_signal_sets_flag` — SIGINT/SIGTERM set stop_flag
    - `test_shutdown_between_cycles` — clean exit with code 0
    - `test_shutdown_interrupts_idle_sleep` — threading.Event wakes immediately on signal
    - `test_double_signal_continues_waiting` — WARNING logged, no force-terminate
    - `test_shutdown_cycle_timeout_warning` — WARNING after 300s with pending shutdown
    - `test_deferred_executes_on_market_open` — same decision used when market opens
    - `test_startup_resumes_pending_decision` — WARNING logged, resumes waiting for market open
    - `test_shutdown_during_deferred_discards` — pending decision cleared, WARNING logged
    - `test_paper_safety_verified` — RuntimeError if URL lacks "paper"
    - `test_credential_error_propagates` — EnvironmentError propagates unchanged
    - `test_state_file_write_failure_halts` — CRITICAL log, sys.exit(1) on disk error
    - _Requirements: 2.10, 2.11, 2.12, 4.1, 4.2, 4.3, 4.5, 4.6, 4.7, 6.9, 9.1, 9.5, 9.6_

- [ ] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP (the AST-scan purity tests in task 4.2 are NOT marked optional — they are required safety-critical tests that run immediately after _create_paper_client is implemented, consistent with how purity tests are treated in data_ingestion, strategy, risk_engine, and execution modules)
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (Properties 1–11)
- Unit tests validate specific examples and edge cases
- The module mirrors the existing `execution/` and `risk_engine/` module layout for developer familiarity
- All code should be explicit and well-commented per the developer's preference (experienced in C++, new to Python)
- Paper trading is enforced via hard-coded `paper=True` literal — never a variable or config value
- Hypothesis property tests configured with `@settings(max_examples=100)` minimum
- Mock Alpaca client and module functions used for all tests — no real network I/O in the test suite
- `threading.Event` is used for interruptible idle waits (not `time.sleep`) to support graceful shutdown within 2 seconds
- State file writes use atomic temp-then-rename pattern to prevent corruption
- Contract violations (stage returning None/unexpected type) halt the loop — they are not retried

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "1.4", "1.5", "1.6", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4"] },
    { "id": 4, "tasks": ["4.5", "4.6", "5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "5.5"] },
    { "id": 6, "tasks": ["7.1"] },
    { "id": 7, "tasks": ["7.2"] },
    { "id": 8, "tasks": ["7.3", "7.4", "7.5"] }
  ]
}
```
