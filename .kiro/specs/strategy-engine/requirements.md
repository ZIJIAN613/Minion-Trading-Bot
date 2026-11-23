# Requirements Document

## Introduction

The `strategy` module is the second stage in the Minions paper-trading pipeline. It consumes historical OHLCV bar DataFrames produced by `data_ingestion` and computes simple moving average (SMA) crossover signals. Specifically, it calculates a short-window SMA and a long-window SMA on the close price and emits one of three signals — BUY, SELL, or HOLD — based on whether the short SMA has just crossed above or below the long SMA. This module performs pure computation only: it has no access to the Alpaca API, no ability to fetch market data, and no ability to place, modify, or cancel orders.

---

## Glossary

- **Strategy_Module**: The `strategy/` Python module described in this document.
- **SMA**: Simple Moving Average — the arithmetic mean of the close price over the most recent N bars.
- **Short_SMA**: The SMA computed over the short window (default 10 bars, configurable).
- **Long_SMA**: The SMA computed over the long window (default 30 bars, configurable).
- **Signal**: One of three string literals: `"BUY"`, `"SELL"`, or `"HOLD"`.
- **Crossover**: The event where the Short_SMA value transitions from being less than or equal to the Long_SMA to being strictly greater than the Long_SMA (bullish crossover) or from being greater than or equal to the Long_SMA to being strictly less than the Long_SMA (bearish crossover).
- **Bar_DataFrame**: A pandas DataFrame with a timezone-aware UTC DatetimeIndex and columns `open` (float64), `high` (float64), `low` (float64), `close` (float64), `volume` (int64) — the canonical schema defined by the `data_ingestion` module.
- **Config_File**: The `config.yaml` file located in the `strategy/` directory containing configurable parameters for this module.
- **Pipeline**: The ordered sequence Data Ingestion → Strategy Engine → Risk Engine → Order Execution.

---

## Requirements

### Requirement 1: Input Validation

**User Story:** As a developer, I want the strategy module to validate incoming DataFrames before processing, so that computation never runs on malformed or insufficient data.

#### Acceptance Criteria

1. WHEN `compute_signal(df)` is called, THE Strategy_Module SHALL first verify that `df` is an instance of `pandas.DataFrame`; IF `df` is not a `pandas.DataFrame` instance (including None), THEN THE Strategy_Module SHALL raise a `TypeError` whose message states the actual type received. IF `df` is a `pandas.DataFrame`, THE Strategy_Module SHALL validate that `df` conforms to the Bar_DataFrame canonical schema (UTC DatetimeIndex, columns `open`, `high`, `low`, `close` as float64, `volume` as int64); IF the schema validation fails, THE Strategy_Module SHALL raise a `ValueError` whose message identifies the first schema mismatch found.
2. IF `df` contains fewer rows than the configured Long_SMA window size + 1 (the minimum needed to compute SMAs for both the current and previous bars), THEN THE Strategy_Module SHALL return a dictionary with `signal` set to `"HOLD"`, `short_sma` set to `None`, `long_sma` set to `None`, `timestamp` set to the DatetimeIndex value of the last row of the input DataFrame (UTC-aware), and — IF a valid `symbol` argument was provided — `symbol` set to that string (following the same inclusion rule as Requirement 6.3). This is not an error condition — it represents normal startup behavior when insufficient history has accumulated.
3. IF any value in the `close` column is NaN, THEN THE Strategy_Module SHALL raise a `ValueError` whose message states the number of NaN values found in the `close` column.
4. IF the DatetimeIndex of `df` is not sorted in strictly ascending chronological order, THEN THE Strategy_Module SHALL raise a `ValueError` whose message states that the index must be sorted in ascending order.
5. IF `df` is a valid `pandas.DataFrame` that passes schema validation but contains zero rows (i.e., `len(df) == 0`), THEN THE Strategy_Module SHALL raise a `ValueError` whose message states that the DataFrame must contain at least one row.

---

### Requirement 2: SMA Computation

**User Story:** As a developer, I want the module to compute short and long simple moving averages on the close price, so that crossover signals can be derived from them.

#### Acceptance Criteria

1. WHEN `compute_signal(df)` is called with a valid Bar_DataFrame whose rows are sorted in ascending chronological order, THE Strategy_Module SHALL compute the Short_SMA for the current bar as the arithmetic mean of the last N close prices (ending at the current bar), where N equals the configured short window size.
2. WHEN `compute_signal(df)` is called with a valid Bar_DataFrame, THE Strategy_Module SHALL compute the Long_SMA for the current bar as the arithmetic mean of the last M close prices (ending at the current bar), where M equals the configured long window size.
3. THE Strategy_Module SHALL also compute the Short_SMA and Long_SMA for the previous bar (the bar immediately preceding the current bar) using the same window sizes, so that crossover detection in Requirement 3 can compare SMAs across two consecutive bars.
4. THE Strategy_Module SHALL compute each SMA value as the sum of the relevant close prices divided by the window size, using floating-point arithmetic with no additional rounding applied.
5. FOR ALL valid Bar_DataFrames with sufficient rows, computing the Short_SMA and Long_SMA and then verifying against an independent calculation (sum of last N close prices divided by N) SHALL produce values equal within a tolerance of 1e-9 (round-trip property).

---

### Requirement 3: Signal Generation

**User Story:** As a developer, I want the module to emit BUY, SELL, or HOLD signals based on SMA crossovers, so that the risk engine can act on clear directional decisions.

#### Acceptance Criteria

1. WHEN the Short_SMA of the current bar is strictly greater than the Long_SMA of the current bar, AND the Short_SMA of the previous bar was less than or equal to the Long_SMA of the previous bar, THE Strategy_Module SHALL return the Signal `"BUY"`.
2. WHEN the Short_SMA of the current bar is strictly less than the Long_SMA of the current bar, AND the Short_SMA of the previous bar was greater than or equal to the Long_SMA of the previous bar, THE Strategy_Module SHALL return the Signal `"SELL"`.
3. WHEN neither a bullish Crossover nor a bearish Crossover has occurred between the previous bar and the current bar (including the case where Short_SMA equals Long_SMA on both bars), THE Strategy_Module SHALL return the Signal `"HOLD"`.
4. THE Strategy_Module SHALL evaluate the Crossover using the last two bars of the provided DataFrame (the current bar and the bar immediately preceding it); this requires the DataFrame to contain at least Long_SMA window size + 1 rows so that both the current and previous bar have valid Long_SMA values. IF the DataFrame has fewer rows, the insufficient-data path in Requirement 1 applies instead.
5. THE Strategy_Module SHALL return exactly one Signal per invocation of `compute_signal` — the Signal is always one of `"BUY"`, `"SELL"`, or `"HOLD"` and no other value.

---

### Requirement 4: Configuration

**User Story:** As a developer, I want the SMA window sizes to be configurable via a YAML file, so that I can adjust the strategy parameters without modifying code.

#### Acceptance Criteria

1. WHEN the Strategy_Module is initialized, THE Strategy_Module SHALL read its runtime configuration from a `config.yaml` file located in the `strategy/` directory.
2. THE Config_File SHALL contain the keys `short_window` and `long_window`, each specifying an integer window size for the Short_SMA and Long_SMA respectively.
3. IF the Config_File is absent, THEN THE Strategy_Module SHALL raise a `FileNotFoundError` with a message identifying the expected file path.
4. IF the Config_File contains invalid YAML syntax, THEN THE Strategy_Module SHALL raise a `ValueError` with a message describing the syntax error.
5. IF the Config_File is valid YAML but is missing the key `short_window` or `long_window`, THEN THE Strategy_Module SHALL raise a `ValueError` identifying each missing key.
6. IF `short_window` or `long_window` is not an integer in the range 1 to 10000 inclusive, THEN THE Strategy_Module SHALL raise a `ValueError` identifying the invalid key and stating the accepted range.
7. IF `short_window` is greater than or equal to `long_window`, THEN THE Strategy_Module SHALL raise a `ValueError` stating that `short_window` must be strictly less than `long_window`.

---

### Requirement 5: Pure Computation Enforcement

**User Story:** As a developer, I want a hard guarantee that the strategy module performs no I/O beyond reading its own config file, so that a bug in this module cannot fetch data or place trades.

#### Acceptance Criteria

1. THE Strategy_Module SHALL NOT import or instantiate any class from the `alpaca.trading`, `alpaca.broker`, or `alpaca.data` packages, nor from any third-party or standard-library module that provides network access (including but not limited to `socket`, `urllib`, `http.client`, `requests`, `httpx`, `websocket`, and `aiohttp`).
2. THE Strategy_Module SHALL NOT import or instantiate any class that exposes methods for placing, modifying, or cancelling orders — specifically, the symbols `TradingClient`, `BrokerClient`, `trading_client`, and `broker_client` SHALL NOT appear in any import statement within the module's source files.
3. THE Strategy_Module SHALL NOT import or invoke `subprocess`, `os.system`, `os.popen`, or any other standard-library facility that spawns external processes.
4. THE Strategy_Module SHALL accept input exclusively through its public function parameters — it SHALL NOT read from files, databases, or network sockets during signal computation. Reading the Config_File during module initialisation (prior to `compute_signal` invocation) is the sole permitted file-read operation.
5. THE Strategy_Module SHALL produce output exclusively through its return value — it SHALL NOT write to files, databases, or network sockets during signal computation. Standard-library logging (the `logging` module) is exempt from this prohibition as it constitutes observability, not functional output.
6. WHEN a static AST scan is performed on all `.py` source files within the `strategy/` directory, THE scan SHALL confirm that no import statement references any symbol or module path listed as forbidden in criteria 1, 2, and 3 above.

---

### Requirement 6: Output Structure

**User Story:** As a developer, I want the strategy module to return a structured result containing both the signal and the computed SMA values, so that downstream modules and logging can inspect the reasoning.

#### Acceptance Criteria

1. WHEN `compute_signal(df)` completes successfully, THE Strategy_Module SHALL return a dictionary containing exactly the following keys and no others (unless `symbol` is provided): `signal` (a Signal string), `short_sma` (a float64 value of the current Short_SMA, or `None` if insufficient data), `long_sma` (a float64 value of the current Long_SMA, or `None` if insufficient data), and `timestamp`.
2. WHEN `compute_signal(df)` completes successfully, THE Strategy_Module SHALL set the `timestamp` value to the pandas Timestamp from the DatetimeIndex of the last row of the input DataFrame, preserving UTC timezone awareness.
3. WHEN the caller provides a `symbol` argument as a non-empty string of at most 10 characters, THE Strategy_Module SHALL include the key `symbol` in the returned dictionary with that string as its value.
4. IF the caller does not provide a `symbol` argument, THEN THE Strategy_Module SHALL omit the `symbol` key from the returned dictionary entirely.
5. IF the caller provides a `symbol` argument that is not a string, is an empty string, or is a string longer than 10 characters, THEN THE Strategy_Module SHALL raise a `ValueError` whose message states that `symbol` must be a non-empty string of at most 10 characters.

---

### Requirement 7: Determinism

**User Story:** As a developer, I want the strategy module to be fully deterministic, so that identical inputs always produce identical outputs and I can replay scenarios for debugging.

#### Acceptance Criteria

1. FOR ALL valid Bar_DataFrames, calling `compute_signal` with the same DataFrame, same configuration, and same `symbol` argument SHALL always produce the same returned dictionary — exact match for `signal`, `timestamp`, and `symbol`; equality within 1e-9 tolerance for `short_sma` and `long_sma`.
2. THE Strategy_Module SHALL not use random number generation, system time, thread-dependent ordering, global mutable state, or any other source of non-determinism during signal computation.
3. THE Strategy_Module SHALL be stateless across invocations — calling `compute_signal` SHALL NOT modify any module-level or instance-level state that could influence the result of a subsequent `compute_signal` call.
