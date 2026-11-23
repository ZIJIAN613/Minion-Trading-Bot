# Implementation Plan: Strategy Engine

## Overview

Implement the `strategy/` module — a pure-computation SMA crossover signal generator. The module receives OHLCV bar DataFrames, computes short and long SMAs on the close price, detects crossover events, and returns a structured signal dictionary. Implementation follows a bottom-up approach: config loading first, then validation, then core computation, then wiring via `__init__.py`, and finally tests.

## Tasks

- [x] 1. Set up module structure and configuration
  - [x] 1.1 Create `strategy/` directory with `__init__.py`, `config.yaml`, and `config_loader.py`
    - Create the `strategy/` directory
    - Create `strategy/config.yaml` with `short_window: 10` and `long_window: 30` (well-commented)
    - Create `strategy/config_loader.py` with the `StrategyConfig` dataclass and `load_config()` function
    - `load_config()` must: check file existence → parse YAML → validate required keys → validate integer range [1, 10000] → validate short < long → return `StrategyConfig`
    - Raise `FileNotFoundError` if file absent, `ValueError` for all other validation failures
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [x]* 1.2 Write property test for config validation (Property 9)
    - **Property 9: Invalid config values always raise ValueError**
    - Generate YAML configs with out-of-range or non-integer values, or short_window >= long_window, and verify `load_config` raises `ValueError` identifying the invalid key
    - **Validates: Requirements 4.5, 4.6, 4.7**

- [x] 2. Implement DataFrame validation
  - [x] 2.1 Create `strategy/validators.py` with `validate_bar_dataframe()`
    - Implement independent schema validation (no imports from `data_ingestion`)
    - Check: DatetimeIndex → UTC timezone → required columns present → column dtypes (float64 for OHLCV prices, int64 for volume)
    - Raise `ValueError` with descriptive message on first mismatch
    - Only import `pandas` and `numpy` — no networking or external module dependencies
    - _Requirements: 1.1, 5.1_

- [x] 3. Implement core signal computation
  - [x] 3.1 Create `strategy/compute.py` with `compute_signal()`
    - Implement the full validation and computation pipeline:
      1. Type check (`TypeError` if not DataFrame)
      2. Schema validation via `validate_bar_dataframe()`
      3. NaN check on close column (`ValueError` with count)
      4. Sort check on DatetimeIndex (`ValueError`)
      5. Empty DataFrame check (`ValueError`)
      6. Symbol argument validation (`ValueError`)
      7. Insufficient data early return (HOLD with None SMAs, and include `symbol` in the dict if a valid `symbol` argument was provided)
      8. SMA computation: mean of last N close prices for current and previous bar
      9. Crossover detection: BUY/SELL/HOLD logic
      10. Build and return result dictionary
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x]* 3.2 Write property test for SMA computation (Property 1)
    - **Property 1: SMA computation matches independent calculation**
    - Generate valid DataFrames with sufficient rows, compute SMAs independently (sum/N), compare with returned values within 1e-9
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

  - [x]* 3.3 Write property test for signal crossover logic (Property 2)
    - **Property 2: Signal matches crossover condition**
    - Generate valid DataFrames, independently compute current/previous SMAs, verify returned signal matches crossover rules exactly
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

  - [x]* 3.4 Write property test for output structure (Property 3)
    - **Property 3: Output structure invariant**
    - For any valid invocation that does not raise, verify keys are exactly {signal, short_sma, long_sma, timestamp} (plus symbol iff provided), timestamp equals df.index[-1] and is UTC-aware, signal is one of BUY/SELL/HOLD
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**

- [x] 4. Checkpoint - Ensure core implementation works
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement error handling property tests
  - [x]* 5.1 Write property test for non-DataFrame TypeError (Property 4)
    - **Property 4: Non-DataFrame input raises TypeError with type name**
    - Generate non-DataFrame values (None, int, str, list, dict), verify `TypeError` is raised with actual type name in message
    - **Validates: Requirements 1.1**

  - [x]* 5.2 Write property test for NaN in close column (Property 5)
    - **Property 5: NaN in close column raises ValueError with count**
    - Generate valid DataFrames, inject NaN into close column, verify `ValueError` message contains exact NaN count
    - **Validates: Requirements 1.3**

  - [x]* 5.3 Write property test for unsorted index (Property 6)
    - **Property 6: Unsorted index raises ValueError**
    - Generate valid DataFrames with 2+ rows, permute index to non-ascending order, verify `ValueError` about ascending order
    - **Validates: Requirements 1.4**

  - [x]* 5.4 Write property test for insufficient data (Property 7)
    - **Property 7: Insufficient data returns HOLD with None SMAs**
    - Generate valid DataFrames with 1 to long_window rows, verify HOLD signal with None SMAs, correct timestamp, and symbol inclusion rule
    - **Validates: Requirements 1.2, 6.3, 6.4**

  - [x]* 5.5 Write property test for determinism (Property 8)
    - **Property 8: Determinism — identical inputs produce identical outputs**
    - Call `compute_signal` twice with same args, verify exact equality for signal/timestamp/symbol and 1e-9 tolerance for SMAs
    - **Validates: Requirements 7.1, 7.3**

  - [x]* 5.6 Write property test for invalid symbol (Property 10)
    - **Property 10: Invalid symbol raises ValueError**
    - Generate invalid symbol values (empty string, None, int, string >10 chars), verify `ValueError` about symbol constraints
    - **Validates: Requirements 6.5**

  - [x]* 5.7 Write property test for empty DataFrame (Property 11)
    - **Property 11: Empty DataFrame raises ValueError**
    - Generate a valid-schema DataFrame with zero rows, verify `ValueError` about needing at least one row
    - **Validates: Requirements 1.5**

- [x] 6. Wire module and add purity enforcement
  - [x] 6.1 Create `strategy/__init__.py` with public API exports
    - Re-export `StrategyConfig`, `load_config`, `compute_signal`, `validate_bar_dataframe`
    - Define `__all__` list
    - _Requirements: 5.1_

  - [x] 6.2 Create `tests/test_strategy_purity.py` with AST-scan smoke test
    - Scan all `.py` files in `strategy/` using Python `ast` module
    - Verify no imports of forbidden modules: `socket`, `urllib`, `http.client`, `requests`, `httpx`, `websocket`, `aiohttp`, `subprocess`, `os.system`, `os.popen`, `alpaca.trading`, `alpaca.broker`, `alpaca.data`, `TradingClient`, `BrokerClient`
    - _Requirements: 5.1, 5.2, 5.3, 5.6_

- [x] 7. Add unit tests for specific scenarios
  - [x]* 7.1 Create `tests/test_strategy_unit.py` with example-based tests
    - `test_config_file_not_found` — verify `FileNotFoundError` with path in message
    - `test_config_invalid_yaml_syntax` — verify `ValueError` on malformed YAML
    - `test_config_loads_default_values` — verify default config.yaml loads as StrategyConfig(10, 30)
    - `test_buy_signal_specific_example` — hand-crafted crossover producing BUY
    - `test_sell_signal_specific_example` — hand-crafted crossover producing SELL
    - `test_hold_signal_no_crossover` — hand-crafted non-crossover producing HOLD
    - _Requirements: 3.1, 3.2, 3.3, 4.1, 4.3, 4.4_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The module uses only `pandas`, `numpy`, and `PyYAML` — no networking libraries
- All test files use Hypothesis (`@given`, `@settings(max_examples=100)`) for property tests
- Test generators should cap DataFrames at 200 rows and window sizes at 200 for performance

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "6.1"] },
    { "id": 4, "tasks": ["5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7", "6.2"] },
    { "id": 5, "tasks": ["7.1"] }
  ]
}
```
