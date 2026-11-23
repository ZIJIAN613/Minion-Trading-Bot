# Implementation Plan: `data_ingestion` Module

## Overview

Implement the `data_ingestion` module for the Minions paper-trading bot in Python.
The module fetches historical OHLCV bars from Alpaca's REST API and streams live
quotes via WebSocket, delivering clean validated pandas DataFrames to the downstream
strategy module. It is strictly read-only — no order placement under any circumstance.

Implementation language: **Python**

---

## Tasks

- [x] 1. Create the `data_ingestion/` package skeleton and testing infrastructure
  - Create `data_ingestion/` directory with `__init__.py` (empty stub for now)
  - Create `data_ingestion/config.yaml` with the documented default values
    (`symbols: [AAPL, SPY]`, `timeframe: "1d"`, `lookback_days: 90`)
  - Create `tests/` directory with `tests/__init__.py`
  - Add `pytest`, `hypothesis`, and `pytest-asyncio` to `requirements.txt`
    (pin exact versions, e.g. `pytest==8.3.5`, `hypothesis==6.131.15`,
    `pytest-asyncio==0.24.0`)
  - _Requirements: 6.1_

- [x] 2. Implement `logger.py` — structured JSON logging
  - [x] 2.1 Implement `get_logger` and `log_structured` in `data_ingestion/logger.py`
    - `get_logger(name)` attaches a `StreamHandler` with a custom `logging.Formatter`
      subclass that serialises the `LogRecord` to a single-line JSON string
      (`{"event": "...", "level": "INFO", "timestamp": "...Z", ...}`)
    - `log_structured(logger, level, event, **fields)` is a thin wrapper that
      calls `logger.log(level, "", extra={"event": event, **fields})` — the
      formatter reads from `extra`
    - Include a comment explaining Python's `**fields` unpacking the first time
      it appears, since this is a Python-specific idiom
    - The logger must never print credential values (enforced by never passing
      them as fields — document this in a code comment)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 1.3_

- [x] 3. Implement `credentials.py` — credential loading
  - [x] 3.1 Implement `load_credentials` in `data_ingestion/credentials.py`
    - Call `dotenv.load_dotenv()` to populate `os.environ` from `.env`
      (add a comment: "context manager-free dotenv call — modifies os.environ
      in place")
    - Loop over `["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]`; if either is
      absent or resolves to an empty string, raise
      `EnvironmentError(f"Missing or empty credential: {key}")`
    - Return `(api_key, secret_key)` as a tuple; never log or print the values
    - _Requirements: 1.1, 1.2, 1.3_

  - [x]* 3.2 Write property test for credential loading
    - **Property 4 (partial): `load_credentials` raises `EnvironmentError`
      for any missing or empty key**
    - Use Hypothesis `@given` with strategies that generate absent/empty env
      states for one or both required variables
    - Verify the error message always names the specific missing key
    - **Validates: Requirements 1.2**

- [x] 4. Implement `timeframe_map.py` — config string to SDK TimeFrame mapping
  - [x] 4.1 Implement `TIMEFRAME_MAP`, `VALID_TIMEFRAMES`, and `to_alpaca_timeframe`
    in `data_ingestion/timeframe_map.py`
    - Define the dict mapping the seven config strings to `TimeFrame` objects
      (comment: "dict literal — Python's equivalent of a `std::unordered_map`")
    - `VALID_TIMEFRAMES` is a `frozenset` of the dict's keys
      (comment: "frozenset is an immutable set — like a `const` set in C++")
    - `to_alpaca_timeframe(timeframe_str)` does a dict lookup and raises
      `ValueError` if the key is not present, listing all accepted values
    - _Requirements: 6.4_

- [x] 5. Implement `schemas.py` — DataFrame schema definitions and validation
  - [x] 5.1 Implement schema constants and `validate_bar_dataframe`,
    `validate_quote_dataframe`, and `make_empty_bar_dataframe`
    in `data_ingestion/schemas.py`
    - Define `BAR_COLUMNS` and `QUOTE_COLUMNS` as module-level dicts
    - `validate_bar_dataframe(df)`: check index is `DatetimeIndex` with UTC tz,
      then iterate columns; raise `ValueError` on the first mismatch, naming the
      column, expected dtype, and actual dtype
    - `validate_quote_dataframe(df)`: same pattern for the quote schema
    - `make_empty_bar_dataframe()`: use `pd.DataFrame(columns=...).astype(...)`
      and attach a UTC `DatetimeIndex`
      (comment: "`.astype()` on an empty frame sets column dtypes without
      needing any rows — handy Python/pandas pattern")
    - _Requirements: 5.1, 5.2, 5.3_

  - [x]* 5.2 Write property test for schema validation — bar DataFrame
    - **Property 9: `validate_bar_dataframe` raises `ValueError` for any
      single-column dtype mismatch, missing column, or non-UTC index**
    - Use Hypothesis strategies to generate DataFrames with exactly one
      deliberate flaw; assert `ValueError` is raised and the message names
      the column
    - **Validates: Requirements 5.3**

  - [x]* 5.3 Write property test for schema validation — quote DataFrame
    - **Property 9 (quote side): `validate_quote_dataframe` raises `ValueError`
      for any single-column dtype mismatch or missing column**
    - Mirror the bar test above, targeting quote schema columns
    - **Validates: Requirements 5.3**

  - [x]* 5.4 Write property test for bar DataFrame JSON round-trip
    - **Property 10: serialise with `df.to_json(orient="split",
      date_format="iso")` and deserialise back; result must equal the original
      in column names, dtypes, index values, and cell values**
    - Use Hypothesis to generate arbitrary valid bar DataFrames (1–50 rows,
      arbitrary float OHLCV values, arbitrary UTC timestamps)
    - **Validates: Requirements 5.4**

- [x] 6. Implement `config_loader.py` — YAML config loading and validation
  - [x] 6.1 Implement `Config` dataclass and `load_config` in
    `data_ingestion/config_loader.py`
    - Use `@dataclass` from the `dataclasses` stdlib module
      (comment: "`@dataclass` is a decorator — it auto-generates `__init__`,
      `__repr__`, etc., similar to a C++ struct with generated boilerplate")
    - Follow Algorithm B from the design exactly: FileNotFoundError →
      YAML parse error → missing keys → symbol list validation →
      timeframe validation → lookback_days validation
    - Re-use `VALID_TIMEFRAMES` from `timeframe_map.py` for the timeframe check
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x]* 6.2 Write property test for symbol list validation
    - **Property 11: `load_config` raises `ValueError` for any symbol list
      containing an empty string or a string longer than 10 characters; does not
      raise for lists where all entries are 1–10 character non-empty strings**
    - Use Hypothesis to generate mixed-valid/invalid symbol lists
    - **Validates: Requirements 6.3**

  - [x]* 6.3 Write property test for timeframe validation
    - **Property 12: `load_config` raises `ValueError` if and only if
      `timeframe` is not in `{"1m", "5m", "15m", "30m", "1h", "4h", "1d"}`**
    - Use Hypothesis `st.text()` to generate arbitrary timeframe strings
    - **Validates: Requirements 6.4**

  - [x]* 6.4 Write property test for lookback_days validation
    - **Property 13: `load_config` raises `ValueError` if and only if
      `lookback_days` is outside `[1, 365]` or is not an integer**
    - Cover integers outside range and non-integer types (float, string)
    - **Validates: Requirements 6.5**

- [x] 7. Implement `rest_client.py` — historical bar fetching
  - [x] 7.1 Implement `RestClient.__init__` and `get_historical_bars`
    in `data_ingestion/rest_client.py`
    - `__init__` instantiates `StockHistoricalDataClient(api_key, secret_key)`
      — comment that this defaults to `data.alpaca.markets`, NOT the trading
      endpoints, enforcing read-only behaviour
    - Import guard: assert that `TradingClient` and `BrokerClient` are NOT
      imported anywhere in this file (enforce via a module-level comment and
      a `RuntimeError` guard function `_guard_no_order_client()`)
    - Follow Algorithm C from the design: validate inputs → build
      `StockBarsRequest` with `feed="iex"` → call `.get_stock_bars()` →
      handle `APIError` → convert `.df` → normalise dtypes → validate schema
      → emit DEBUG log → return
    - Emit `ERROR` log on API failure before re-raising `IOError`
      (Requirement 7.5)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 4.1, 4.2, 7.2, 7.5_

  - [x]* 7.2 Write property test for bar output schema
    - **Property 1: for any valid (symbol, timeframe, start, end) combination,
      with a mocked Alpaca client returning a non-empty response, the returned
      DataFrame has a UTC-aware `DatetimeIndex` and correct column dtypes**
    - Use Hypothesis to generate valid argument combinations; mock
      `StockHistoricalDataClient` to return a canned response
    - **Validates: Requirements 2.1, 5.1, 5.3**

  - [x]* 7.3 Write property test for non-2xx HTTP error propagation
    - **Property 2: for any HTTP status code in 400–599, `get_historical_bars`
      raises `IOError` whose message contains that status code**
    - Use Hypothesis `st.integers(min_value=400, max_value=599)` to generate
      error codes; mock the SDK to raise `APIError` with each code
    - **Validates: Requirements 2.3**

  - [x]* 7.4 Write property test for paginated response aggregation
    - **Property 3: for any sequence of N pages of K bars each, the returned
      DataFrame has exactly N × K rows with no bars dropped or duplicated**
    - Use Hypothesis to generate page counts (1–10) and bar counts per page
      (1–100); mock the SDK to simulate paginated responses
    - **Validates: Requirements 2.5**

  - [x]* 7.5 Write property test for invalid input `ValueError`
    - **Property 4: for any invalid input (empty symbol, unsupported timeframe,
      or start ≥ end), `get_historical_bars` raises `ValueError` whose message
      contains the name of the offending argument**
    - Use Hypothesis to generate each invalid case independently
    - **Validates: Requirements 2.6**

  - [x] 7.6 Write smoke test `tests/test_read_only.py` — static import scan
    - Create `tests/test_read_only.py` with a single test function
      `test_no_forbidden_imports()`
    - The test uses Python's `ast` stdlib module to parse every `.py` file
      under `data_ingestion/` into an Abstract Syntax Tree
      (comment: "`ast.parse()` reads source code as a tree of nodes — like
      Clang's AST but built into Python's stdlib; no compilation needed")
    - Walk each AST with `ast.walk()` and collect all `Import` and
      `ImportFrom` nodes
      (comment: "`ast.walk()` is a generator that yields every node in the
      tree depth-first — Python's equivalent of a recursive tree traversal")
    - Assert that none of the collected import names contain `TradingClient`,
      `BrokerClient`, or the strings `trading_client`, `broker_client`
    - On failure, the assertion message must name the offending file and the
      forbidden symbol found, so the developer knows exactly where to look
    - This test requires no mocking and no network access — it runs purely on
      source text, so it is always fast and deterministic
    - _Requirements: 4.1, 4.2_

- [x] 8. Checkpoint — core data path complete
  - Ensure all tests pass up to this point, ask the user if questions arise.

- [x] 9. Implement `stream_client.py` — live quote streaming
  - [x] 9.1 Implement `StreamClient` skeleton: `__init__`, `subscribe`,
    `unsubscribe`, `disconnect`, and `_handle_quote`
    in `data_ingestion/stream_client.py`
    - `__init__` builds `StockDataStream(api_key, secret_key)` but does NOT
      open the connection yet
    - Maintain a `self._subscribed_symbols: set[str]` instance variable
      (comment: "Python `set` — like `std::unordered_set`; tracks active subs")
    - Maintain a `self._reconnect_attempt: int` counter for back-off logging
    - `subscribe` registers `_handle_quote` via
      `self._stream.subscribe_quotes()` and starts the stream in a background
      thread using `threading.Thread(target=self._stream.run, daemon=True)`
      (comment: "`daemon=True` means the thread dies when the main program
      exits — useful for background I/O loops")
    - Follow Algorithm D from the design for all methods
    - `_handle_quote` must never raise; malformed messages log a WARNING and
      return silently (Requirement 3.7)
    - `disconnect` calls `self._stream.stop()` and clears `_subscribed_symbols`
    - _Requirements: 3.1, 3.2, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2_

  - [x] 9.2 Implement exponential back-off reconnect logging
    - Implement `back_off_delay(attempt: int) -> float` per Algorithm E
      (`min(1.0 * 2**(attempt-1), 60.0)`)
    - Hook into the SDK's on-connect callback to detect reconnects, increment
      `self._reconnect_attempt`, compute the delay, and emit the WARNING log
      with `event="websocket_reconnecting"`, `symbols`, `attempt`,
      `backoff_seconds` (Requirement 7.3)
    - Reset `self._reconnect_attempt` to 0 on the first successful connect
    - _Requirements: 3.3, 7.3_

  - [x]* 9.3 Write property test for quote callback DataFrame correctness
    - **Property 5: for any quote message with arbitrary symbol string and
      non-negative bid/ask float values, `_handle_quote` invokes the callback
      with a single-row DataFrame that has correct columns, correct dtypes, and
      `mid_price == (bid_price + ask_price) / 2`**
    - Use Hypothesis to generate (symbol, bid, ask) triples; drive
      `_handle_quote` directly with a mock data object
    - **Validates: Requirements 3.2, 5.2, 5.3**

  - [x]* 9.4 Write property test for exponential back-off bounds
    - **Property 6: for any reconnect attempt number N ≥ 1,
      `back_off_delay(N)` satisfies `delay >= 1.0`, `delay <= 60.0`,
      and `delay == min(2**(N-1), 60.0)`**
    - Use Hypothesis `st.integers(min_value=1, max_value=100)` to cover
      all saturation points
    - **Validates: Requirements 3.3**

  - [x]* 9.5 Write property test for malformed quote handling
    - **Property 7: for any message object missing required fields or with
      wrong-typed fields, `_handle_quote` does NOT call the user callback and
      does NOT raise; it logs exactly one WARNING**
    - Use Hypothesis to generate malformed dicts (missing keys, wrong types)
    - **Validates: Requirements 3.7**

  - [x]* 9.6 Write property test for `unsubscribe` safety
    - **Property 8: for any set of subscribed symbols S and any unsubscribe
      set U (where U may be a superset of S), `unsubscribe(U)` stops callbacks
      for S ∩ U and silently ignores U \ S without raising**
    - Use Hypothesis `st.sets(st.text(...))` to generate S and U
    - **Validates: Requirements 3.5**

- [x] 10. Implement `__init__.py` — `DataIngestionModule` public API
  - [x] 10.1 Implement `DataIngestionModule` class in `data_ingestion/__init__.py`
    - `initialise()`: call `load_credentials()`, then `load_config()`, build
      `RestClient` and `StreamClient`, emit INFO log `"module_initialised"`
      with `symbols` and `timeframe` (Requirement 7.1)
    - `get_historical_bars(symbol, timeframe, start, end)`: delegate to
      `self._rest_client.get_historical_bars()`
    - `subscribe(symbols, callback)`: delegate to
      `self._stream_client.subscribe()`
    - `unsubscribe(symbols)`: delegate to `self._stream_client.unsubscribe()`
    - `disconnect()`: delegate to `self._stream_client.disconnect()`
    - Wrap all public methods with a top-level `try/except Exception` that emits
      the ERROR log `"unhandled_exception"` with `exception_type`, `message`,
      and `stack_trace` (use `traceback.format_exc()`) before re-raising
      (Requirement 7.4)
    - _Requirements: 1.1, 1.2, 1.3, 4.3, 7.1, 7.4_

  - [x]* 10.2 Write unit tests for `DataIngestionModule.initialise` error paths
    - Test that `EnvironmentError` is raised when credentials are missing
    - Test that `FileNotFoundError` is raised when `config.yaml` is missing
    - Test that a successful `initialise()` emits the INFO log with correct fields
    - _Requirements: 1.1, 1.2, 6.2_

- [x] 11. Checkpoint — full module wired together
  - Ensure all unit tests and property tests pass; run `pytest tests/ -v`.
  - Ask the user if any behaviour needs adjustment before the final review.

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
  The core functionality (tasks 1–10.1) is fully usable without them.
- Each task references specific requirements for traceability.
- Property tests use the [Hypothesis](https://hypothesis.readthedocs.io/) library.
  Run them with `pytest tests/ -v` — Hypothesis integrates with pytest natively.
- The C++ developer note: Python's `pytest` is the equivalent of Google Test or
  Catch2. `@pytest.fixture` (decorator) provides setup/teardown like `SetUp()`
  in a test class. No `main()` needed — pytest discovers tests by file and
  function name conventions (`test_*.py`, `def test_*()`).
- `data_ingestion/` is strictly read-only. `TradingClient` and `BrokerClient`
  must never be imported anywhere in this module.
- Paper trading only via Alpaca. No live account credentials or endpoints.

---

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["2.1", "4.1"] },
    { "id": 1, "tasks": ["3.1", "5.1"] },
    { "id": 2, "tasks": ["3.2", "5.2", "5.3", "5.4", "6.1"] },
    { "id": 3, "tasks": ["6.2", "6.3", "6.4", "7.1"] },
    { "id": 4, "tasks": ["7.2", "7.3", "7.4", "7.5", "7.6"] },
    { "id": 5, "tasks": ["9.1"] },
    { "id": 6, "tasks": ["9.2"] },
    { "id": 7, "tasks": ["9.3", "9.4", "9.5", "9.6"] },
    { "id": 8, "tasks": ["10.1"] },
    { "id": 9, "tasks": ["10.2"] }
  ]
}
```
