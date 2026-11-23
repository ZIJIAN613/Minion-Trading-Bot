# Implementation Plan: Risk Engine

## Overview

Implement the `risk_engine/` module as the third stage of the Minions paper-trading pipeline. The module receives a signal dictionary, current market price, and account snapshot, applies four configurable guardrails (max position size, max daily loss, max open positions, mandatory stop-loss), and returns either an approved order or a rejection. The implementation mirrors the existing `strategy/` module structure for consistency.

## Tasks

- [x] 1. Set up module structure and configuration
  - [x] 1.1 Create `risk_engine/` directory with `__init__.py`, `config.yaml`, `config_loader.py`, `validators.py`, and `evaluate.py`
    - Create the directory structure matching the design
    - Create `risk_engine/__init__.py` that re-exports `RiskConfig`, `load_config`, and `evaluate_signal`
    - Create `risk_engine/config.yaml` with default values: `max_position_percent: 5.0`, `max_daily_loss: 500.0`, `max_open_positions: 5`, `stop_loss_percent: 2.0`
    - _Requirements: 7.1, 7.2_

  - [x] 1.2 Implement `risk_engine/config_loader.py` with `RiskConfig` dataclass and `load_config()` function
    - Define `RiskConfig` dataclass with fields: `max_position_percent` (float), `max_daily_loss` (float), `max_open_positions` (int), `stop_loss_percent` (float)
    - Implement `load_config(config_path: str) -> RiskConfig` following the 6-step algorithm: file existence check → YAML parsing → non-dict check → required key presence → value type/range validation → return RiskConfig
    - Explicitly reject booleans for all numeric fields (Python quirk: `bool` is subclass of `int`)
    - Raise `FileNotFoundError` if config file absent, `ValueError` for all other config issues
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ]* 1.3 Write property test for config validation (Property 18)
    - **Property 18: Invalid config values raise ValueError**
    - **Validates: Requirements 7.5, 7.6, 7.7**

- [x] 2. Implement input validators
  - [x] 2.1 Implement `risk_engine/validators.py` with `validate_signal_dict()`, `validate_current_price()`, and `validate_account_snapshot()`
    - `validate_signal_dict`: check dict type → check `signal` key in {"BUY","SELL","HOLD"} → check `timestamp` is UTC-aware pd.Timestamp → check optional `symbol` is 1–10 char string
    - `validate_current_price`: check is int/float (not bool, not None) and > 0
    - `validate_account_snapshot`: check dict type → check `equity` (positive float) → check `cash` (non-negative float) → check `open_positions` (list of dicts with `symbol` non-empty str and `size` positive int) → check `daily_realized_pnl` (float)
    - Follow fail-fast pattern: first invalid item raises immediately
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [ ]* 2.2 Write property test for validation order priority (Property 1)
    - **Property 1: Validation order priority**
    - **Validates: Requirements 1.1**

  - [ ]* 2.3 Write property test for non-dict signal_dict (Property 2)
    - **Property 2: Non-dict signal_dict raises TypeError with type name**
    - **Validates: Requirements 1.2**

  - [ ]* 2.4 Write property test for invalid signal_dict content (Property 3)
    - **Property 3: Invalid signal_dict content raises ValueError**
    - **Validates: Requirements 1.3, 1.4, 1.5**

  - [ ]* 2.5 Write property test for invalid current_price (Property 4)
    - **Property 4: Invalid current_price raises ValueError**
    - **Validates: Requirements 1.6**

  - [ ]* 2.6 Write property test for invalid account_snapshot (Property 5)
    - **Property 5: Invalid account_snapshot raises TypeError or ValueError**
    - **Validates: Requirements 1.7**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement core evaluation logic
  - [x] 4.1 Implement `risk_engine/evaluate.py` with the `evaluate_signal()` function — HOLD pass-through and daily loss check
    - Implement the function signature: `evaluate_signal(signal_dict, current_price, account_snapshot, config) -> dict`
    - Call validators in defined order: `validate_signal_dict` → `validate_current_price` → `validate_account_snapshot`
    - Implement Step 4 (HOLD pass-through): return NO_ACTION Decision_Dict immediately, include `symbol` key only if present in signal_dict
    - Implement Step 5 (daily loss check): if `daily_realized_pnl <= -max_daily_loss`, return VETOED with reason including both numeric values
    - _Requirements: 2.1, 2.2, 4.1, 4.2, 4.3, 9.1_

  - [x] 4.2 Implement AST-scan smoke test for forbidden imports in `tests/test_risk_engine_purity.py`
    - Two-pass scan: Pass 1 walks `ast.Import`/`ast.ImportFrom` for forbidden modules (socket, urllib, requests, httpx, alpaca.trading, subprocess, etc.)
    - Pass 2 walks `ast.Call` nodes for forbidden function calls (eval, exec, __import__, os.system, os.popen, importlib.import_module)
    - Scan all `.py` files in the `risk_engine/` directory
    - This test runs early so every subsequent implementation step is immediately validated against the no-order-placement constraint
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [ ]* 4.3 Write property test for HOLD pass-through (Property 6)
    - **Property 6: HOLD pass-through ignores account state**
    - **Validates: Requirements 2.1, 2.2**

  - [ ]* 4.4 Write property test for daily loss veto (Property 7)
    - **Property 7: Daily loss exceeded vetoes BUY and SELL**
    - **Validates: Requirements 4.2, 4.3**

  - [x] 4.5 Implement max open positions check (BUY) and position-held verification (SELL) in `evaluate_signal()`
    - BUY: if `len(open_positions) >= max_open_positions` → VETO with count and limit in reason
    - SELL: if no `symbol` key in signal_dict → VETO with reason about missing symbol
    - SELL: if `symbol` not found in open_positions with size > 0 → VETO with reason about position not held
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [ ]* 4.6 Write property test for BUY max open positions veto (Property 8)
    - **Property 8: BUY vetoed when max open positions reached**
    - **Validates: Requirements 5.2, 5.6**

  - [ ]* 4.7 Write property test for SELL position-held verification (Property 9)
    - **Property 9: SELL requires held position and is never blocked by max positions**
    - **Validates: Requirements 5.3, 5.4, 5.5**

  - [x] 4.8 Implement position sizing logic in `evaluate_signal()`
    - BUY: compute `max_allowed_capital = equity * (max_position_percent / 100)`, then `position_size = min(floor(max_allowed_capital / current_price), floor(cash / current_price))`
    - BUY: if position_size == 0, VETO with appropriate reason (equity-percent limit is binding constraint if `floor(max_allowed_capital / price) == 0`, otherwise insufficient cash)
    - SELL: set `position_size` equal to the `size` of the matching position in open_positions
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [ ]* 4.9 Write property test for BUY position sizing formula (Property 10)
    - **Property 10: BUY position sizing formula**
    - **Validates: Requirements 3.2, 3.3, 3.5, 3.6**

  - [ ]* 4.10 Write property test for SELL position sizing (Property 11)
    - **Property 11: SELL position sizing uses full held size**
    - **Validates: Requirements 3.4**

  - [x] 4.11 Implement stop-loss computation and final APPROVED return in `evaluate_signal()`
    - BUY: `stop_loss = round(current_price * (1 - stop_loss_percent / 100), 2)` — if stop_loss <= 0, VETO
    - SELL: `stop_loss = round(current_price * (1 + stop_loss_percent / 100), 2)` — always valid
    - Return APPROVED Decision_Dict with all required keys, include `symbol` only if present in signal_dict
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ]* 4.12 Write property test for BUY stop-loss computation (Property 12)
    - **Property 12: BUY stop-loss computation**
    - **Validates: Requirements 6.2, 6.4, 6.5**

  - [ ]* 4.13 Write property test for SELL stop-loss computation (Property 13)
    - **Property 13: SELL stop-loss computation**
    - **Validates: Requirements 6.3, 6.4, 6.6**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement cross-cutting property tests
  - [ ]* 6.1 Write property test for output structure invariant (Property 14)
    - **Property 14: Output structure invariant**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**

  - [ ]* 6.2 Write property test for risk check ordering (Property 15)
    - **Property 15: Risk check ordering**
    - **Validates: Requirements 9.1, 9.2, 9.3**

  - [ ]* 6.3 Write property test for determinism (Property 16)
    - **Property 16: Determinism**
    - **Validates: Requirements 11.1, 11.2, 11.3**

  - [ ]* 6.4 Write property test for no input mutation (Property 17)
    - **Property 17: No input mutation**
    - **Validates: Requirements 11.4**

- [x] 7. Write example-based unit tests
  - [x]* 7.1 Write unit tests in `tests/test_risk_engine_unit.py`
    - `test_config_file_not_found` — verifies FileNotFoundError with path in message
    - `test_config_invalid_yaml_syntax` — verifies ValueError on malformed YAML
    - `test_config_loads_default_values` — verifies default config.yaml loads correctly
    - `test_sell_without_symbol_vetoed` — SELL signal missing symbol key
    - `test_approved_buy_specific_example` — hand-crafted scenario producing APPROVED BUY
    - `test_approved_sell_specific_example` — hand-crafted scenario producing APPROVED SELL
    - `test_multiple_vetoes_reports_first` — scenario validating check order
    - _Requirements: 1.1–1.7, 2.1, 3.3, 3.4, 4.2, 5.4, 6.2, 6.3, 7.3, 7.4, 9.2_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The module mirrors the existing `strategy/` module layout for developer familiarity
- All code should be explicit and well-commented per the developer's preference (experienced in C++, new to Python)
- The `risk_engine/` module must NEVER import networking or subprocess libraries (Requirement 10)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "2.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6"] },
    { "id": 4, "tasks": ["4.1"] },
    { "id": 5, "tasks": ["4.2", "4.3", "4.4", "4.5"] },
    { "id": 6, "tasks": ["4.6", "4.7", "4.8"] },
    { "id": 7, "tasks": ["4.9", "4.10", "4.11"] },
    { "id": 8, "tasks": ["4.12", "4.13"] },
    { "id": 9, "tasks": ["6.1", "6.2", "6.3", "6.4", "7.1"] }
  ]
}
```
