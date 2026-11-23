# Implementation Plan: File-Based Logging System

## Overview

Centralized file-based logging for the Minions paper-trading bot. Moves the existing `_JsonFormatter` and `get_logger()` from `data_ingestion/logger.py` into a new shared `log_system/` module, adds a `SafeFileHandler` for per-run log files under `logs/`, and integrates all six pipeline modules with structured decision-reconstruction logging.

## Tasks

- [ ] 1. Create shared log_system module with core formatter and logger
  - [x] 1.1 Create `log_system/__init__.py` with public exports and `log_system/formatter.py` with `_JsonFormatter` (moved from `data_ingestion/logger.py`)
    - Create `log_system/` directory at project root
    - Move `_JsonFormatter` class into `log_system/formatter.py` (verbatim from `data_ingestion/logger.py`)
    - Add `module` field to the formatter output (read from `record.__dict__`)
    - Ensure `timestamp` always ends with `Z` suffix (ISO 8601 UTC)
    - Create `log_system/__init__.py` that exports `get_logger`, `log_structured`, `initialize_file_logging`
    - _Requirements: 5.1, 5.2, 5.4, 8.1, 8.2_

  - [x] 1.2 Create `log_system/logger.py` with `get_logger()` and `log_structured()`
    - Implement `get_logger(name)` that attaches stderr `StreamHandler` with `_JsonFormatter`
    - Set `logger._log_system_managed = True` sentinel on every logger created
    - If file handler already exists in module state, attach it immediately
    - Implement `log_structured(logger, level, event, **fields)` that requires `event` as positional arg (TypeError if omitted)
    - Auto-inject `module` field from `logger.name`
    - _Requirements: 1.1, 1.2, 5.2, 5.3, 5.5_

  - [ ] 1.3 Write property tests for formatter output validity (Property 1) and extra fields round-trip (Property 2)
    - **Property 1: Formatter output is valid structured JSON with mandatory fields**
    - **Property 2: Extra fields round-trip through the formatter**
    - File: `tests/test_log_system_properties.py`
    - Use Hypothesis to generate random event strings, levels, module names, and extra field dicts
    - Assert output is valid single-line JSON with `event`, `level`, `timestamp`, `module` keys
    - Assert `timestamp` ends with `Z` and is valid ISO 8601
    - Assert every input extra field appears in parsed JSON output
    - **Validates: Requirements 1.2, 5.1, 5.2, 5.4, 5.5, 8.1, 8.2**

- [ ] 2. Implement SafeFileHandler and file initialization
  - [ ] 2.1 Create `log_system/file_handler.py` with `SafeFileHandler` class
    - Subclass `logging.FileHandler`
    - Override `emit()` to wrap `super().emit()` in try/except for `OSError`
    - Implement `_file_healthy` flag — on first failure, set to `False` and emit WARNING to stderr via `_emit_stderr_warning()` (direct `sys.stderr.write` of JSON, bypassing logging framework)
    - Implement `_alert_dispatched` flag for double-failure guard
    - Implement `_dispatch_double_failure_alert()` that calls `alerting.sender.send_alert()` with `_suppress_logging=True`
    - Reset `_file_healthy = True` on successful writes (recovery case)
    - _Requirements: 1.3, 1.4, 7.1, 7.3_

  - [ ] 2.2 Implement `initialize_file_logging()` in `log_system/file_handler.py`
    - Create `logs/` directory if it does not exist (catch `OSError` for permissions/disk)
    - Generate filename as `YYYYMMDD_HHMMSS_{entry_point}.log` using UTC time
    - Create `SafeFileHandler` with the generated filepath
    - Attach `_JsonFormatter` to the file handler
    - Store handler in module-level state for future `get_logger()` calls
    - Retroactively iterate `logging.Logger.manager.loggerDict` and attach handler to all loggers with `_log_system_managed = True`
    - Return `True` on success, `False` on failure (with WARNING to stderr)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 7.2_

  - [ ] 2.3 Validate and sanitize `entry_point` at the start of `initialize_file_logging()`
    - Validate `entry_point` FIRST, before constructing the file path or creating the `logs/` directory
    - Reject anything containing OS path separators or other unsafe characters, so a malicious/careless `entry_point` cannot escape the `logs/` directory (path traversal like `../`, `/`, `\`, or a drive-letter prefix)
    - Follow the project's existing input-validation convention (as in `config_loader.py`): raise a `ValueError` with a clear message for invalid input. Keep the check explicit and well-commented (developer is new to Python) — e.g. iterate/inspect the string with a small allow-list of characters (alphanumerics + underscore) rather than a terse regex one-liner
    - Add a `# NOTE:` comment explaining WHY this matters: `entry_point` becomes part of a filesystem path, so unvalidated input is a path-traversal risk
    - This protects the filename invariant that Property 5 depends on (basename matches `^\d{8}_\d{6}_.+\.log$` and the file stays inside `logs/`)
    - _Requirements: 2.1_
    - _Property: 5_

  - [ ] 2.4 Write unit test for invalid `entry_point` validation
    - File: `tests/test_log_system_unit.py`
    - Test that `initialize_file_logging()` raises `ValueError` (or sanitizes, matching the chosen implementation in 2.3) when `entry_point` contains a path separator (e.g. `"../evil"`, `"foo/bar"`, `"a\\b"`)
    - Test that a valid `entry_point` (e.g. `"fetch_aapl"`) is accepted and produces a filename whose basename stays inside `logs/`
    - **Validates: Requirements 2.1**
    - **Property: 5**

  - [ ] 2.5 Write property tests for filename generation (Property 5) and file failure isolation (Property 4)
    - **Property 5: Filename generation produces valid filenames**
    - **Property 4: File write failure does not propagate exceptions**
    - File: `tests/test_log_system_properties.py`
    - Use Hypothesis to generate random entry_point strings (alphanum + underscore) and random UTC datetimes
    - Assert basename matches regex `^\d{8}_\d{6}_.+\.log$`
    - Mock file handler to raise `OSError`; assert no exception propagates to caller and WARNING emitted to stderr
    - **Validates: Requirements 2.1, 2.2, 9.2, 1.3, 7.1**

  - [ ] 2.6 Write unit tests for initialization and fallback behavior
    - File: `tests/test_log_system_unit.py`
    - Test `logs/` directory creation on first call
    - Test `PermissionError` fallback returns `False` and emits WARNING to stderr
    - Test console-only fallback when file creation fails
    - Test retroactive handler attachment to loggers created before initialization
    - Test two consecutive runs produce distinct log files
    - _Requirements: 2.4, 2.5, 3.2, 7.2_

- [ ] 3. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Implement config loading, dual output, and backward-compatibility shim
  - [ ] 4.1 Create `log_system/config_loader.py` with `load_log_level()`
    - Define `LoggingConfig` dataclass with `log_level: str`
    - Implement `load_log_level(config_path)` that reads `log_level` from YAML
    - Accept values case-insensitively (normalize to uppercase)
    - Default to `"INFO"` when field is absent, `None`, or unrecognized
    - Log WARNING to stderr for invalid values
    - _Requirements: 4.1, 4.2, 4.3_

  - [ ] 4.2 Apply configured log level as minimum threshold for both handlers
    - Update `get_logger()` or provide a helper to set the logger's effective level
    - Ensure both file handler and console handler respect the same configured level (no individual handler overrides stricter than the configured level)
    - _Requirements: 4.4_

  - [ ] 4.3 Convert `data_ingestion/logger.py` to a backward-compatibility shim
    - Replace the entire file contents with re-exports from `log_system`
    - Re-export `get_logger`, `log_structured`, and `_JsonFormatter`
    - Verify existing `from data_ingestion.logger import get_logger` imports continue to work across the codebase
    - _Requirements: 1.2_

  - [ ] 4.4 Write property tests for log level case-insensitivity (Property 6) and level threshold filtering (Property 7)
    - **Property 6: Log level acceptance is case-insensitive**
    - **Property 7: Level threshold filters entries correctly**
    - File: `tests/test_log_system_properties.py`
    - Use Hypothesis to generate random case permutations of valid level strings
    - Assert `load_log_level()` returns uppercase-normalized form
    - Test all 25 combinations of (configured_level, entry_level) — entry emitted iff severity >= configured
    - **Validates: Requirements 4.2, 4.4**

  - [ ] 4.5 Write unit tests for dual output and event requirement
    - File: `tests/test_log_system_unit.py`
    - Test `log_structured()` without `event` raises `TypeError`
    - Test that log entries reach both stderr handler and file handler (Property 3 coverage)
    - Test `module` field in output equals logger name
    - **Property 3: Dual output delivers to both handlers with correct module field**
    - **Validates: Requirements 1.1, 2.3, 5.3**

- [ ] 5. Modify alerting module for `_suppress_logging` support
  - [ ] 5.1 Add `_suppress_logging` keyword parameter to `alerting/sender.py` `send_alert()`
    - Add `_suppress_logging: bool = False` keyword-only parameter to `send_alert()` signature
    - Guard all internal `logger.warning()` calls behind `if not _suppress_logging`
    - Ensure alerts still dispatch correctly (HTTP call still fires) when `_suppress_logging=True`
    - _Requirements: 1.4_

  - [ ] 5.2 Write unit tests for `_suppress_logging` parameter
    - File: `tests/test_log_system_unit.py` or extend existing `tests/test_alerting*.py`
    - Test that `send_alert(..., _suppress_logging=True)` does NOT invoke any logger calls
    - Test that `send_alert(..., _suppress_logging=True)` still attempts HTTP dispatch
    - Test that existing behavior (without flag) is unchanged (no regressions)
    - _Requirements: 1.4_

- [ ] 6. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Integrate decision-reconstruction logging into pipeline modules
  - [ ] 7.1 Add structured logging to Risk Engine (`risk_engine/evaluate.py` or equivalent)
    - Import `get_logger` and `log_structured` from `log_system`
    - Call `log_structured()` with event `"risk_evaluation"` after each signal evaluation
    - Include fields: signal, signal_timestamp, symbol, current_price, account_equity, account_cash, open_positions_count, daily_realized_pnl, config values, decision, decision_reason, position_size, stop_loss
    - _Requirements: 6.1_

  - [ ] 7.2 Add structured logging to Execution module (`execution/execute.py` or equivalent)
    - Import `get_logger` and `log_structured` from `log_system`
    - Call `log_structured()` with event `"order_filled"` (or `"order_submitted"`) after order submission/confirmation
    - Include fields: symbol, signal, position_size, entry_order_id, stop_loss_order_id, fill_price, filled_quantity, filled_at, status
    - _Requirements: 6.2_

  - [ ] 7.3 Add structured logging to Strategy module (`strategy/compute.py` or equivalent)
    - Import `get_logger` and `log_structured` from `log_system`
    - Call `log_structured()` with event `"signal_generated"` after producing a signal
    - Include fields: symbol, signal, short_sma, long_sma, timestamp
    - _Requirements: 6.3_

  - [ ] 7.4 Add structured logging to Data Ingestion (`data_ingestion/rest_client.py`)
    - Import `get_logger` and `log_structured` from `log_system`
    - Call `log_structured()` with event `"fetch_complete"` after each fetch completes
    - Include fields: symbol, timeframe, bars_retrieved, date_start, date_end
    - _Requirements: 6.4_

  - [ ] 7.5 Write integration tests for decision-reconstruction logging (Requirement 6)
    - File: `tests/test_log_system_integration.py`
    - Test Risk Engine log entries contain all required fields (signal, price, account snapshot, decision)
    - Test Execution log entries contain order parameters, fill details, status
    - Test Strategy log entries contain indicator values and signal direction
    - Test Data Ingestion log entries contain symbol, timeframe, bar count, date range
    - Use a test fixture that captures log output and asserts field presence
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [ ] 8. Wire initialization into entry points and finalize
  - [ ] 8.1 Call `initialize_file_logging("orchestrator")` in Orchestrator entry point (`orchestrator/loop.py`)
    - Add call early in `run_loop()` (or equivalent entry function) before pipeline execution begins
    - Handle `False` return gracefully (log WARNING, continue with console-only)
    - _Requirements: 2.1, 2.3_

  - [ ] 8.2 Wire `initialize_file_logging()` into the standalone scripts
    - Each of these three project-root scripts currently uses `get_logger()` and can be run on its own, so each needs its own file-logging bootstrap: `fetch_aapl.py`, `run_pipeline.py`, `run_strategy.py`
    - Near the top of each script's entry point (inside the `if __name__ == "__main__":` block, before any pipeline work begins), add a call to `initialize_file_logging(entry_point="<script_name>")` so that standalone runs land under `logs/` with their own filename
    - Pass each script's own name as `entry_point`: `"fetch_aapl"` in `fetch_aapl.py`, `"run_pipeline"` in `run_pipeline.py`, `"run_strategy"` in `run_strategy.py`
    - Handle a `False` return gracefully (the run continues with console-only logging), mirroring the orchestrator wiring in 8.1
    - Add a brief comment in each script explaining that this call bootstraps per-run file logging and must run before the first log entry (developer is new to Python)
    - _Requirements: 2.1_

  - [ ] 8.3 Add `log_level` field to each module's `config.yaml` and wire config loading
    - Add optional `log_level: "INFO"` to `data_ingestion/config.yaml`, `strategy/config.yaml`, `risk_engine/config.yaml`, `execution/config.yaml`, `orchestrator/config.yaml`, `alerting/config.yaml`
    - Call `load_log_level()` during module initialization and apply to logger
    - _Requirements: 4.1, 4.3_

  - [ ] 8.4 Add `logs/` to `.gitignore`
    - Append `logs/` entry to the project's `.gitignore` file
    - _Requirements: 9.1, 9.2_

  - [ ] 8.5 Write unit tests for .gitignore and double-failure alert path
    - File: `tests/test_log_system_unit.py`
    - Test `logs/` entry exists in `.gitignore` after setup
    - Test double-failure scenario: mock `sys.stderr.write` to raise `OSError`, then trigger file write failure — confirm `_dispatch_double_failure_alert()` is called and `send_alert()` invoked with `_suppress_logging=True`
    - Test that `_alert_dispatched` flag prevents duplicate alerts within a run
    - _Requirements: 1.4, 9.1_

  - [ ] 8.6 Write unit test asserting `_emit_stderr_warning()` output is valid JSON
    - File: `tests/test_log_system_unit.py`
    - Capture the actual string written via `sys.stderr.write()` during a simulated file-write failure (e.g. patch `sys.stderr` with an object that records what is written, then trigger a `SafeFileHandler` file-write `OSError`)
    - Assert the captured string parses as valid JSON (via `json.loads`) and that the parsed object contains the keys `event`, `level`, `timestamp`, and `module`
    - **IMPORTANT — this is distinct from Property 1's formatter tests**: `_emit_stderr_warning()` hand-builds its JSON payload with `json.dumps(...)` and bypasses `_JsonFormatter` entirely (see the design's Error Handling section). Property 1 only exercises `_JsonFormatter`, so the stderr-warning payload is NOT covered by it and needs its own assertion here
    - Add a comment noting this distinction so a future reader does not mistake it for a duplicate of the Property 1 tests
    - _Requirements: 1.3, 5.1, 5.2_

- [ ] 9. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties defined in the design
- Unit tests validate specific examples and edge cases
- The backward-compatibility shim (`data_ingestion/logger.py`) ensures zero breaking changes to existing imports
- The `_suppress_logging` addition to `alerting/sender.py` is the only modification to existing shipped code outside the logging module itself

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1", "4.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.2", "5.1"] },
    { "id": 3, "tasks": ["2.4", "2.5", "2.6", "4.3", "4.4", "4.5", "5.2"] },
    { "id": 4, "tasks": ["7.1", "7.2", "7.3", "7.4", "8.1", "8.2", "8.3", "8.4"] },
    { "id": 5, "tasks": ["7.5", "8.5", "8.6"] }
  ]
}
```
