# Requirements Document

## Introduction

Centralized file-based logging for the Minions paper-trading bot. All pipeline modules (Data Ingestion, Strategy, Risk Engine, Execution) and supporting modules (Orchestrator, Alerting) persist structured log output to files under a `logs/` directory at the project root. This enables post-run reconstruction of decisions and debugging without ad-hoc print statements, while preserving existing console (stderr) output.

## Glossary

- **Logging_System**: The centralized file-logging subsystem that manages log file creation, formatting, writing, and configuration for all modules.
- **Module**: One of the six components in the Minions bot — Data Ingestion, Strategy, Risk Engine, Execution, Orchestrator, or Alerting.
- **Run**: A single invocation of the Orchestrator loop or a standalone module script, from start to termination.
- **Log_Entry**: A single structured record written to a log file, containing an event name, level, UTC timestamp, and contextual fields.
- **Log_File**: A file under `logs/` that stores all Log_Entry records produced during one Run, across all participating Modules.
- **Log_Level**: One of five severity thresholds — DEBUG, INFO, WARNING, ERROR, CRITICAL — controlling which Log_Entry records are emitted.
- **Config_Loader**: The existing pattern (YAML file + `config_loader.py` returning a validated dataclass) used by each Module to load configuration.

## Requirements

### Requirement 1: Dual Output — File and Console

**User Story:** As a developer, I want all modules to write log entries to both stderr and a log file, so that I can monitor the bot in real time via the console and review logs from disk after a run.

#### Acceptance Criteria

1. WHEN a Module emits a Log_Entry, THE Logging_System SHALL write that Log_Entry to both the console (stderr) handler and the corresponding Log_File.
2. THE Logging_System SHALL preserve the existing stderr JSON-structured output format without modification.
3. IF the Log_File write fails, THEN THE Logging_System SHALL emit a WARNING-level entry to stderr and continue pipeline execution without interruption.
4. IF both the Log_File write and stderr output fail simultaneously, THEN THE Logging_System SHALL allow the calling Module to continue processing without interruption and SHALL attempt to dispatch a `logging_both_destinations_failed` alert via the existing Alerting module's `send_alert()` mechanism without blocking on the outcome of that dispatch.

---

### Requirement 2: Per-Run Log File Naming

**User Story:** As a developer, I want each run to produce a uniquely named log file, so that I can unambiguously identify which file corresponds to a specific run.

#### Acceptance Criteria

1. WHEN a Run starts, THE Logging_System SHALL create a single shared Log_File named with the pattern `{UTC_timestamp}_{entry_point}.log` inside the `logs/` directory, where `entry_point` is `orchestrator` for a full Orchestrator run or the standalone script's name when a single module is run on its own.
2. THE Logging_System SHALL format the UTC timestamp component as `YYYYMMDD_HHMMSS` (e.g., `20250101_143052_orchestrator.log`).
3. ALL Modules participating in a Run SHALL write their Log_Entry records to the same shared Log_File, disambiguated by the mandatory `module` field on each Log_Entry.
4. THE Logging_System SHALL create the `logs/` directory if it does not already exist at the project root.
5. IF Log_File creation fails due to permissions or disk space, THEN THE Logging_System SHALL emit a WARNING to stderr and allow the Run to continue with console-only logging.

---

### Requirement 3: Unlimited Retention

**User Story:** As a developer, I want every log file retained indefinitely, so that I can go back to any historical run without worrying about automatic deletion.

#### Acceptance Criteria

1. THE Logging_System SHALL retain all Log_File files without rotation, deletion, or size-based pruning.
2. THE Logging_System SHALL create a new Log_File for each Run rather than appending to an existing file.

---

### Requirement 4: Configurable Log Level

**User Story:** As a developer, I want to configure the minimum log level via config.yaml, so that I can control verbosity without modifying code.

#### Acceptance Criteria

1. THE Logging_System SHALL read a `log_level` field from the module's `config.yaml` file using the existing Config_Loader pattern.
2. THE Logging_System SHALL accept the values DEBUG, INFO, WARNING, ERROR, and CRITICAL for the `log_level` field (case-insensitive).
3. WHEN the `log_level` field is absent from config.yaml, THE Logging_System SHALL default to INFO.
4. THE Logging_System SHALL apply the configured log level as the exact minimum threshold for both the file handler and the console handler; individual handlers SHALL NOT use stricter thresholds than the configured level.

---

### Requirement 5: Structured JSON Log Format

**User Story:** As a developer, I want log entries written as single-line JSON objects, so that I can grep, parse, and reconstruct events programmatically while still being able to skim them in a text editor.

#### Acceptance Criteria

1. THE Logging_System SHALL write each Log_Entry to the Log_File as a single-line JSON object (JSON Lines format).
2. THE Logging_System SHALL include at minimum the fields: `event`, `level`, `timestamp`, and `module` in every Log_Entry.
3. THE Logging_System's logging function SHALL require `event` as a mandatory parameter, such that omitting it raises a TypeError at the call site during development rather than producing an incomplete Log_Entry at runtime. The fields `level`, `timestamp`, and `module` SHALL be supplied internally by the Logging_System and SHALL never be absent.
4. THE Logging_System SHALL format the `timestamp` field as an ISO 8601 UTC string with a `Z` suffix (e.g., `2025-01-15T14:30:52Z`).
5. THE Logging_System SHALL serialize additional contextual fields (passed via keyword arguments) into the same JSON object alongside the mandatory fields.

---

### Requirement 6: Decision-Reconstruction Detail

**User Story:** As a developer, I want each pipeline stage to log the actual inputs evaluated and the resulting decision, so that I can reconstruct any decision after the fact without ad-hoc print statements.

#### Acceptance Criteria

1. WHEN the Risk Engine evaluates a signal, THE Logging_System SHALL record the signal details, current price, account snapshot fields, applicable config values, and the resulting approval or rejection decision as fields in the Log_Entry.
2. WHEN the Execution module submits or receives confirmation of an order, THE Logging_System SHALL record the order parameters, fill details, and final status as fields in the Log_Entry.
3. WHEN the Strategy module produces a signal, THE Logging_System SHALL record the indicator values used and the resulting signal direction as fields in the Log_Entry.
4. WHEN the Data Ingestion module completes a fetch regardless of whether data was returned, THE Logging_System SHALL record the symbol, timeframe, number of bars retrieved, and date range as fields in the Log_Entry.

---

### Requirement 7: Non-Blocking and Crash-Safe

**User Story:** As a developer, I want logging failures to never block or crash the pipeline, so that the trading loop remains operational even when disk writes fail.

#### Acceptance Criteria

1. IF a Log_File write raises an exception, THEN THE Logging_System SHALL catch the exception, emit a WARNING to stderr, and allow the calling Module to continue without interruption.
2. IF the `logs/` directory cannot be created, THEN THE Logging_System SHALL emit a WARNING to stderr and fall back to console-only logging for the duration of the Run.
3. THE Logging_System SHALL impose no blocking I/O on the main pipeline thread beyond the time needed to write a single Log_Entry to disk.

---

### Requirement 8: UTC Timestamps

**User Story:** As a developer, I want all log timestamps to be UTC-aware, so that timestamps are consistent with the rest of the project's timestamp invariant.

#### Acceptance Criteria

1. THE Logging_System SHALL produce all `timestamp` values as UTC-aware datetime objects formatted with the `Z` suffix; timestamps that are internally UTC-correct but missing the `Z` suffix in their string representation SHALL be treated as invalid.
2. THE Logging_System SHALL not use local timezone offsets in any Log_Entry timestamp and SHALL always include the `Z` suffix when formatting timestamps as strings.

---

### Requirement 9: Git-Ignored Log Directory

**User Story:** As a developer, I want the `logs/` directory to be git-ignored, so that log files are treated as local runtime artifacts and never committed to version control.

#### Acceptance Criteria

1. THE Logging_System SHALL ensure `logs/` is present in the project's `.gitignore` file.
2. THE Logging_System SHALL place all Log_File files exclusively inside the `logs/` directory at the project root.
