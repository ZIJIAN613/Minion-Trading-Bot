# Implementation Plan: Execution Module

## Overview

Implement the `execution/` module as the fourth and final active stage of the Minions paper-trading pipeline. The module receives a `Decision_Dict` from the `risk_engine`, validates it, and — only for APPROVED decisions — places the corresponding order via the Alpaca paper-trading API. BUY signals submit bracket orders (market entry + stop-loss); SELL signals submit plain market sell orders. The module polls for fill confirmation and returns a structured `Order_Result`. Implementation uses Python with explicit, well-commented code (developer experienced in C++, new to Python). Property-based testing uses Hypothesis.

## Tasks

- [x] 1. Set up module structure and configuration
  - [x] 1.1 Create `execution/` directory with `__init__.py`, `config.yaml`, and `config_loader.py`
    - Create `execution/` directory at the project root
    - Create `execution/config.yaml` with default values: `fill_timeout_seconds: 30`, `poll_interval_seconds: 1.0` and explanatory comments
    - Create `execution/__init__.py` with module-level docstring forbidding live trading, re-exporting `ExecutionConfig`, `load_config`, and `execute_decision` via `__all__`
    - Create `execution/config_loader.py` with `ExecutionConfig` dataclass (fields: `fill_timeout_seconds: float`, `poll_interval_seconds: float`) and `load_config(config_path: str) -> ExecutionConfig` following the 7-step algorithm: file existence → YAML parsing → non-dict check → required key presence → value type/range validation (booleans rejected) → warn on unknown keys → return ExecutionConfig
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 10.1, 10.2, 10.5, 10.6_

  - [ ]* 1.2 Write property test for config validation (Property 7)
    - **Property 7: Config validation rejects invalid values**
    - **Validates: Requirements 8.2, 8.6, 8.7**
    - Test in `tests/test_execution_properties.py`
    - Use Hypothesis strategies generating: missing keys, out-of-range values, boolean values, non-numeric types
    - Verify `load_config` raises `ValueError` with the invalid key name in the message

  - [ ]* 1.3 Write unit tests for config edge cases
    - Test in `tests/test_execution_unit.py`
    - `test_config_file_not_found` — verifies `FileNotFoundError` with path in message
    - `test_config_invalid_yaml_syntax` — verifies `ValueError` on malformed YAML
    - `test_config_loads_default_values` — verifies default `execution/config.yaml` loads correctly
    - `test_config_unknown_keys_warning` — verifies unknown keys logged at WARNING, no error raised
    - _Requirements: 8.3, 8.4, 8.5, 8.8_

- [x] 2. Implement input validators
  - [x] 2.1 Implement `execution/validators.py` with `validate_decision_dict()`
    - Implement strict sequential validation (fail-fast): Step 1 dict type check → Step 2 `decision` key → Step 3 `signal` key → Step 4 `timestamp` (UTC-aware pd.Timestamp) → Step 5 `reason` (non-empty string 1–500 chars) → Step 6 (APPROVED only) `position_size` (positive int ≥ 1, booleans rejected) → Step 7 (APPROVED only) `stop_loss` (positive float > 0.0, booleans rejected) → Step 8 (APPROVED only) `symbol` (non-empty string 1–10 chars)
    - Raise `TypeError` for non-dict input (Step 1), `ValueError` for all other validation failures
    - Include explicit comments explaining each step and the Python `bool`-is-`int` quirk
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 10.3_

  - [ ]* 2.2 Write property test for non-dict input (Property 1)
    - **Property 1: Non-dict input raises TypeError with type name**
    - **Validates: Requirements 1.1**
    - Test in `tests/test_execution_properties.py`
    - Use Hypothesis `st.from_type(type).flatmap(st.from_type)` filtering out dicts, verify `TypeError` raised with type name in message

  - [ ]* 2.3 Write property test for validation order (Property 2)
    - **Property 2: Validation order is strictly sequential (fail-fast)**
    - **Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6, 1.7**
    - Test in `tests/test_execution_properties.py`
    - Generate dicts valid up to step N-1 but invalid at step N, verify the correct error is raised for step N and no later step is checked

- [x] 3. Implement core execution logic — non-approved paths and paper safety
  - [x] 3.1 Implement `execution/execute.py` — imports, logger, `_create_paper_client()`, and non-approved decision handling
    - Set up imports: `logging`, `sys`, `time`, `datetime`, `pandas`, `alpaca.trading.client.TradingClient`, `alpaca.trading.requests`, `alpaca.trading.enums`, `data_ingestion.credentials.load_credentials`, local `config_loader` and `validators`
    - Create logger named `"execution"`
    - Implement `_create_paper_client()`: load credentials via `data_ingestion.credentials.load_credentials()`, instantiate `TradingClient(api_key, secret_key, paper=True)` with inline comment "NEVER change this", verify base URL contains "paper" → raise `RuntimeError` if not
    - Implement `execute_decision()` entry: call `validate_decision_dict()` (let TypeError/ValueError propagate), then handle VETOED/NO_ACTION (log `decision_skipped` event, return `Order_Result` with `status="skipped"` and `position_size=0`), then handle APPROVED+HOLD guard (return skipped)
    - _Requirements: 2.1, 2.2, 2.3, 3.6, 6.1, 6.2, 6.3, 6.5, 7.6, 7.7, 9.1, 9.2, 9.3, 9.4_

  - [ ]* 3.2 Write property test for non-approved decisions (Property 3)
    - **Property 3: Non-approved decisions return skipped with no API interaction**
    - **Validates: Requirements 2.1, 2.2, 2.3**
    - Test in `tests/test_execution_properties.py`
    - Generate valid VETOED/NO_ACTION Decision_Dicts with varying fields, pass a mock client, verify `status="skipped"` returned and mock never called

  - [ ]* 3.3 Write unit tests for paper safety and credential handling
    - Test in `tests/test_execution_unit.py`
    - `test_paper_safety_base_url_check` — mock TradingClient with non-paper URL, verify `RuntimeError`
    - `test_credential_error_propagated` — mock `load_credentials` to raise `EnvironmentError`, verify it propagates
    - `test_approved_hold_returns_skipped` — APPROVED+HOLD Decision_Dict returns skipped
    - _Requirements: 6.1, 6.2, 6.3, 9.4_

  - [x]* 3.4 Write AST-scan safety tests for paper=True enforcement (early)
    - Test in `tests/test_execution_safety.py`
    - `test_paper_true_ast_scan` — AST scan all `.py` files in `execution/` verifying `paper=True` in every `TradingClient` instantiation, no `paper=False`, no variable used for `paper` parameter
    - `test_init_docstring_forbids_live` — verify `__init__.py` module docstring mentions live trading is forbidden
    - _Requirements: 6.4, 6.5_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement order submission logic (BUY bracket and SELL market)
  - [x] 5.1 Implement `_submit_buy_bracket()` and `_submit_sell_market()` in `execution/execute.py`
    - `_submit_buy_bracket(client, symbol, quantity, stop_loss_price)`: submit bracket order with `order_class="bracket"`, market buy entry (`time_in_force="day"`), stop-loss sell leg at `stop_loss_price` (`time_in_force="gtc"`). If bracket rejected by Alpaca, fall back to: plain market buy → wait for fill → submit separate stop order for filled quantity. Return dict with `entry_order_id`, `stop_loss_order_id`, `bracket_fallback`.
    - `_submit_sell_market(client, symbol, quantity)`: submit plain market sell order (`time_in_force="day"`, no stop-loss, no bracket). Return dict with `entry_order_id`, `stop_loss_order_id=None`.
    - Add structured INFO-level logging for each order submission event
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 7.1_

  - [ ]* 5.2 Write property test for BUY bracket order construction (Property 4)
    - **Property 4: BUY bracket order construction**
    - **Validates: Requirements 3.1, 3.3, 3.4, 4.1, 4.2**
    - Test in `tests/test_execution_properties.py`
    - Generate valid APPROVED BUY Decision_Dicts with varying symbol/position_size/stop_loss, mock client captures submitted order params, verify bracket structure

  - [ ]* 5.3 Write property test for SELL plain market order construction (Property 5)
    - **Property 5: SELL plain market order construction**
    - **Validates: Requirements 3.2, 3.7, 4.7**
    - Test in `tests/test_execution_properties.py`
    - Generate valid APPROVED SELL Decision_Dicts, mock client, verify plain market sell with no stop-loss and no bracket class

  - [ ]* 5.4 Write unit tests for bracket fallback and edge cases
    - Test in `tests/test_execution_unit.py`
    - `test_bracket_order_rejected_fallback` — mock bracket rejection, verify fallback to plain market + separate stop, `bracket_fallback=True`
    - `test_fallback_stop_loss_submission_fails` — entry placed but stop-loss fails → `status="error"`
    - _Requirements: 4.3, 4.5, 4.6_

- [x] 6. Implement fill polling logic
  - [x] 6.1 Implement `_poll_fill()` in `execution/execute.py`
    - Poll at `config.poll_interval_seconds` intervals using `time.monotonic()` for elapsed tracking
    - Terminal states: `"filled"`, `"partially_filled"`, `"cancelled"`, `"expired"`, `"rejected"`
    - Clamp sleep to remaining time budget (never overshoot timeout)
    - Final check after loop: query once more, if still non-terminal → cancel order → return `status="timeout"`
    - Handle network error during polling: re-query once by order ID
    - Return dict with `status`, `fill_price`, `filled_quantity`, `filled_at`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 12.3, 12.6_

  - [ ]* 6.2 Write unit tests for polling scenarios
    - Test in `tests/test_execution_unit.py`
    - `test_order_filled_result_structure` — mock immediate fill, verify Order_Result fields
    - `test_partial_fill_at_timeout` — mock partial fill at timeout, verify cancellation and `partial_fill` result
    - `test_order_cancelled_before_fill` — mock cancelled status, verify terminal state handling
    - `test_order_timeout_cancel` — mock never-filling order, verify timeout → cancel → result
    - `test_network_error_during_polling_requery` — mock network error then successful re-query
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 12.3_

- [x] 7. Wire execution flow together and implement error handling
  - [x] 7.1 Complete `execute_decision()` — wire submission, polling, Order_Result construction, and error wrapping
    - After validation and non-approved handling: create/reuse Alpaca paper client → call `_submit_buy_bracket` or `_submit_sell_market` based on signal → call `_poll_fill` → build complete `Order_Result` dict
    - Wrap all post-validation logic in try/except: catch any exception, log ERROR event, return `Order_Result` with `status="error"`, `error_type` = exception class name, and `entry_order_id` reflecting submission state
    - Add structured logging for fill confirmation (`order_filled`), partial fills (`partial_fill_accepted`), terminal states (`order_terminal`), and API errors (`api_error`)
    - Implement logging failure fallback: catch exceptions from `logger.*` calls, write to `sys.stderr`, continue
    - _Requirements: 5.1, 5.2, 5.6, 5.7, 7.1, 7.2, 7.3, 7.4, 7.5, 7.8, 7.9, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 12.1, 12.2, 12.4, 12.5, 12.7_

  - [ ]* 7.2 Write property test for Order_Result structure invariant (Property 6)
    - **Property 6: Order_Result structure invariant**
    - **Validates: Requirements 11.1, 11.4**
    - Test in `tests/test_execution_properties.py`
    - For any invocation that passes validation, verify returned dict always has base keys, `status` is one of eight defined values, `reason` is 1–500 chars, `timestamp` is UTC-aware pd.Timestamp

  - [ ]* 7.3 Write property test for unexpected exceptions caught (Property 8)
    - **Property 8: Unexpected exceptions are caught and returned as error results**
    - **Validates: Requirements 12.4**
    - Test in `tests/test_execution_properties.py`
    - Generate valid APPROVED Decision_Dicts, mock client raising various exception types, verify `status="error"` returned (never propagated)

  - [ ]* 7.4 Write property test for entry_order_id reflects submission state (Property 9)
    - **Property 9: entry_order_id reflects submission state**
    - **Validates: Requirements 12.7**
    - Test in `tests/test_execution_properties.py`
    - Mock client raising before vs after order ID assignment, verify `entry_order_id` is `None` vs present accordingly

  - [ ]* 7.5 Write unit tests for error handling and logging
    - Test in `tests/test_execution_unit.py`
    - `test_http_error_during_submission` — mock HTTP 4xx/5xx, verify error result with HTTP details
    - `test_logging_failure_swallowed` — mock logger to raise, verify `sys.stderr` fallback and execution continues
    - `test_log_event_order_submitted` — verify structured INFO log on order submission
    - `test_log_event_order_filled` — verify structured INFO log on fill
    - `test_log_event_decision_skipped` — verify structured INFO log for non-approved
    - _Requirements: 7.1, 7.2, 7.5, 7.9, 12.1, 12.2_

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement remaining safety tests (require full execute_decision wired up)
  - [x]* 9.1 Write remaining safety tests in `tests/test_execution_safety.py`
    - `test_no_direct_env_reading` — AST scan verifying `execution/` does not import `dotenv` or read `.env` directly
    - `test_no_retry_on_submission_failure` — mock Alpaca client to raise on first `submit_order`, verify `execute_decision` returns `status="error"` and mock called exactly once (no retries)
    - _Requirements: 9.2, 12.5_

- [x] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (Properties 1–9)
- Unit tests validate specific examples and edge cases
- The module mirrors the existing `risk_engine/` and `strategy/` module layout for developer familiarity
- All code should be explicit and well-commented per the developer's preference (experienced in C++, new to Python)
- The `execution/` module is the ONLY module permitted to place, modify, or cancel orders
- Paper trading is enforced via hard-coded `paper=True` literal — never a variable or config value
- Hypothesis property tests configured with `@settings(max_examples=100)` minimum
- Mock Alpaca client used for all tests — no real network I/O in the test suite

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 4, "tasks": ["5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "6.1"] },
    { "id": 6, "tasks": ["6.2", "7.1"] },
    { "id": 7, "tasks": ["7.2", "7.3", "7.4", "7.5"] },
    { "id": 8, "tasks": ["9.1"] }
  ]
}
```
