# Requirements Document

## Introduction

The `data_ingestion` module is the entry point of the Minions paper-trading pipeline. It is responsible for two tasks: fetching historical OHLCV candle data from Alpaca's REST API and streaming live price quotes via Alpaca's WebSocket feed. All output is delivered as clean, validated pandas DataFrames to the downstream strategy module. The module is strictly read-only — it must never place, modify, or cancel any order under any circumstance.

---

## Glossary

- **Data_Ingestion_Module**: The `data_ingestion/` Python module described in this document.
- **REST_Client**: The component responsible for authenticated HTTP requests to the Alpaca historical data REST API.
- **WebSocket_Client**: The component responsible for establishing and maintaining a persistent WebSocket connection to the Alpaca live market data feed.
- **DataFrame**: A pandas `DataFrame` object with a DatetimeIndex and typed columns (open, high, low, close, volume).
- **OHLCV**: Open, High, Low, Close, Volume — the standard fields of a price candle.
- **Symbol**: A valid equity ticker string (e.g., `"AAPL"`, `"SPY"`).
- **Timeframe**: A candle bar interval supported by Alpaca (e.g., `"1Min"`, `"1Hour"`, `"1Day"`).
- **Quote**: A real-time bid/ask/last price update delivered via WebSocket.
- **Alpaca_API**: The Alpaca Markets paper-trading REST and WebSocket API, accessed via credentials in `.env`.
- **Pipeline**: The ordered sequence Data Ingestion → Strategy Engine → Risk Engine → Order Execution.
- **Callback**: A Python callable (function) passed to the WebSocket_Client that is invoked with a DataFrame whenever a new Quote arrives.
- **Credentials**: The `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` values loaded from the `.env` file.

---

## Requirements

### Requirement 1: Credential Loading

**User Story:** As a developer, I want the module to load Alpaca API credentials from the `.env` file at startup, so that secrets are never hard-coded in source files.

#### Acceptance Criteria

1. WHEN the Data_Ingestion_Module initialises, THE Data_Ingestion_Module SHALL load the environment variables `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` from the `.env` file using `python-dotenv`.
2. IF the `.env` file is missing, OR IF either `APCA_API_KEY_ID` or `APCA_API_SECRET_KEY` is absent from the file or resolves to an empty string, THEN THE Data_Ingestion_Module SHALL raise an `EnvironmentError` whose message identifies the name of the missing or empty key.
3. THE Data_Ingestion_Module SHALL NOT log or print the value of any Credential at any log level.

---

### Requirement 2: Historical Candle Fetching (REST)

**User Story:** As a developer, I want to fetch historical OHLCV candles for a given Symbol and Timeframe over a date range, so that the strategy module has back-history to compute indicators.

#### Acceptance Criteria

1. WHEN `get_historical_bars(symbol, timeframe, start, end)` is called with valid inputs — where `symbol` is a non-empty string, `timeframe` is one of the accepted values (see Requirement 6, Criterion 4), and `start` and `end` are ISO 8601 date strings with `start` strictly before `end` — THE REST_Client SHALL return a DataFrame with a timezone-aware UTC DatetimeIndex and columns `open`, `high`, `low`, `close`, `volume` with dtypes `float64` for price columns and `int64` for volume.
2. WHEN `get_historical_bars` is called, THE REST_Client SHALL send requests to the Alpaca Market Data API base URL (`https://data.alpaca.markets`) and SHALL NOT send requests to the trading API URLs (`https://api.alpaca.markets` or `https://paper-api.alpaca.markets`).
3. IF the Alpaca API returns a non-2xx HTTP status code, THEN THE REST_Client SHALL raise an `IOError` whose message contains the HTTP status code and the response body text.
4. IF the requested date range returns zero bars, THEN THE REST_Client SHALL return an empty DataFrame with the correct column schema (columns `open`, `high`, `low`, `close` as float64 and `volume` as int64, zero rows, with a UTC timezone-aware DatetimeIndex).
5. WHEN the Alpaca API response is paginated, THE REST_Client SHALL follow all pagination tokens and return a single DataFrame containing every bar across all pages for the requested range.
6. IF `symbol` is an empty string, OR IF `timeframe` is not one of the accepted values, OR IF `start` is greater than or equal to `end`, THEN THE REST_Client SHALL raise a `ValueError` whose message identifies the invalid argument by name.

---

### Requirement 3: Live Price Streaming (WebSocket)

**User Story:** As a developer, I want to subscribe to live price quotes for one or more Symbols via WebSocket, so that the strategy module can react to real-time market movements.

#### Acceptance Criteria

1. WHEN `subscribe(symbols, callback)` is called, THE WebSocket_Client SHALL establish an authenticated WebSocket connection to the Alpaca paper-trading data stream endpoint; IF the connection cannot be established due to a network error before authentication, THE WebSocket_Client SHALL raise an `ConnectionError` with a descriptive message.
2. WHEN a Quote message is received for a subscribed Symbol, THE WebSocket_Client SHALL invoke `callback` with a single-row DataFrame whose columns are `symbol` (str), `bid_price` (float64), `ask_price` (float64), `mid_price` (float64, computed as `(bid_price + ask_price) / 2`), `timestamp` (datetime64[ns, UTC]).
3. IF the WebSocket connection drops unexpectedly (i.e., not via an explicit `disconnect()` call), THEN THE WebSocket_Client SHALL attempt to reconnect using exponential back-off starting at 1 second and capped at 60 seconds, re-subscribing to all previously subscribed Symbols after each successful reconnect.
4. IF authentication is rejected by the Alpaca API, THEN THE WebSocket_Client SHALL raise a `PermissionError` with a descriptive message and SHALL NOT attempt to reconnect.
5. WHEN `unsubscribe(symbols)` is called for a set of Symbols, THE WebSocket_Client SHALL stop delivering Quote callbacks for those Symbols without closing the connection for any remaining subscribed Symbols; IF `unsubscribe` is called with a Symbol that is not currently subscribed, THE WebSocket_Client SHALL silently ignore that Symbol.
6. WHEN `disconnect()` is called, THE WebSocket_Client SHALL close the WebSocket connection cleanly and stop all callbacks.
7. IF a message received over the WebSocket connection cannot be parsed or does not conform to the expected Quote schema, THEN THE WebSocket_Client SHALL log a WARNING event (see Requirement 7) with the raw message content and SHALL NOT invoke the Callback or raise an exception.

---

### Requirement 4: Read-Only Enforcement

**User Story:** As a developer, I want a hard guarantee that the data ingestion module can never place, modify, or cancel an order, so that a bug in this module cannot cause unintended trades.

#### Acceptance Criteria

1. THE Data_Ingestion_Module SHALL only call Alpaca API endpoints whose purpose is to retrieve market data or account information; it SHALL NOT call any endpoint whose purpose is to create, modify, or cancel an order.
2. THE Data_Ingestion_Module SHALL NOT import or instantiate any class that exposes methods for placing, modifying, or cancelling orders, regardless of the class name or SDK version.
3. IF any code path in the Data_Ingestion_Module attempts to call a function for placing, modifying, or cancelling an order, THE Data_Ingestion_Module SHALL raise a `RuntimeError` at the call site with an error message indicating that the module is read-only and order operations are prohibited.

---

### Requirement 5: Output Schema Consistency

**User Story:** As a developer, I want every DataFrame produced by the module to have a consistent, documented schema, so that the strategy module can consume data without defensive type-checking.

#### Acceptance Criteria

1. THE Data_Ingestion_Module SHALL define a single canonical schema for historical bar DataFrames: a timezone-aware UTC DatetimeIndex, and columns `open` (float64), `high` (float64), `low` (float64), `close` (float64), `volume` (int64).
2. THE Data_Ingestion_Module SHALL define a single canonical schema for live quote DataFrames: columns `symbol` (str/object), `bid_price` (float64), `ask_price` (float64), `mid_price` (float64), `timestamp` (datetime64[ns, UTC]).
3. WHEN a DataFrame is produced, THE Data_Ingestion_Module SHALL validate that it conforms to the declared schema (correct column names, correct dtypes, correct index type) before returning it or passing it to a Callback; IF validation fails, THE Data_Ingestion_Module SHALL raise a `ValueError` whose message identifies the first schema mismatch found (column name, expected dtype, actual dtype).
4. FOR ALL valid historical bar responses from the Alpaca API, serialising the resulting DataFrame to JSON and deserialising it back SHALL produce a DataFrame that is equal to the original in column names, dtypes, index values, and cell values (round-trip property).

---

### Requirement 6: Configuration

**User Story:** As a developer, I want data-ingestion behaviour (symbols to track, timeframe, history window) to be configurable without code changes, so that I can adjust the bot's parameters quickly.

#### Acceptance Criteria

1. THE Data_Ingestion_Module SHALL read its runtime configuration (list of Symbols to subscribe, default Timeframe, history look-back window in days) from a `config.yaml` file located in the `data_ingestion/` directory.
2. IF the `config.yaml` file is absent, THEN THE Data_Ingestion_Module SHALL raise a `FileNotFoundError` with a message identifying the expected file path. IF the file contains invalid YAML syntax, THE Data_Ingestion_Module SHALL raise a `ValueError` with a message identifying the syntax error. IF the file is valid YAML but is missing one or more required keys (`symbols`, `timeframe`, or `lookback_days`), THE Data_Ingestion_Module SHALL raise a `ValueError` identifying each missing key.
3. WHERE a Symbol list is specified in `config.yaml`, THE Data_Ingestion_Module SHALL validate that each entry is a non-empty string of 1 to 10 characters; IF any entry is invalid, THE Data_Ingestion_Module SHALL raise a `ValueError` identifying the offending entry and its position in the list.
4. WHERE a `timeframe` value is specified in `config.yaml`, THE Data_Ingestion_Module SHALL validate that it is one of the accepted values: `"1m"`, `"5m"`, `"15m"`, `"30m"`, `"1h"`, `"4h"`, `"1d"`; IF the value is not in this set, THE Data_Ingestion_Module SHALL raise a `ValueError` identifying the invalid value and listing the accepted values.
5. WHERE a `lookback_days` value is specified in `config.yaml`, THE Data_Ingestion_Module SHALL validate that it is an integer in the range 1 to 365 inclusive; IF the value is outside this range or is not an integer, THE Data_Ingestion_Module SHALL raise a `ValueError` identifying the invalid value and the accepted range.

---

### Requirement 7: Logging Integration

**User Story:** As a developer, I want the data ingestion module to emit structured log events at key lifecycle points, so that the logging module can observe and record its activity.

#### Acceptance Criteria

1. WHEN the Data_Ingestion_Module initialises successfully, THE Data_Ingestion_Module SHALL emit a structured log event at INFO level containing the fields: `event` (string `"module_initialised"`), `level` (string `"INFO"`), `timestamp` (UTC ISO 8601), `symbols` (list of loaded Symbol strings), and `timeframe` (the configured Timeframe string).
2. WHEN a historical data fetch completes successfully, THE Data_Ingestion_Module SHALL emit a structured log event at DEBUG level containing the fields: `event` (`"historical_fetch_complete"`), `level` (`"DEBUG"`), `timestamp` (UTC ISO 8601), `symbol`, `timeframe`, `start`, `end`, and `bar_count` (integer number of bars returned).
3. WHEN the WebSocket_Client reconnects after a dropped connection, THE Data_Ingestion_Module SHALL emit a structured log event at WARNING level containing the fields: `event` (`"websocket_reconnecting"`), `level` (`"WARNING"`), `timestamp` (UTC ISO 8601), `symbols` (list of subscribed Symbol strings), `attempt` (integer reconnection attempt number, starting at 1), and `backoff_seconds` (numeric back-off delay in seconds).
4. IF any unhandled exception propagates out of the Data_Ingestion_Module, THE Data_Ingestion_Module SHALL emit a structured log event at ERROR level containing the fields: `event` (`"unhandled_exception"`), `level` (`"ERROR"`), `timestamp` (UTC ISO 8601), `exception_type`, `message`, and `stack_trace`; THE Data_Ingestion_Module SHALL then re-raise the exception.
5. IF a historical data fetch fails due to an API error or network error, THE Data_Ingestion_Module SHALL emit a structured log event at ERROR level containing the fields: `event` (`"historical_fetch_failed"`), `level` (`"ERROR"`), `timestamp` (UTC ISO 8601), `symbol`, `timeframe`, `start`, `end`, `error_type`, and `error_message`; THE Data_Ingestion_Module SHALL then raise the appropriate exception as defined in Requirement 2.
