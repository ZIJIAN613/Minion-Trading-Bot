# Design Document: `data_ingestion` Module

## Overview

The `data_ingestion` module is the first stage in the Minions pipeline. Its single
responsibility is to retrieve market price data from Alpaca and hand clean,
validated pandas DataFrames to the strategy module downstream. It does **nothing
else** — it does not compute indicators, manage positions, or place orders.

### What it does

| Responsibility | Mechanism |
|---|---|
| Load credentials | `python-dotenv` reads `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` from `.env` |
| Read configuration | `PyYAML` reads `data_ingestion/config.yaml` |
| Fetch historical OHLCV bars | Alpaca REST via `StockHistoricalDataClient` |
| Stream live quotes | Alpaca WebSocket via `StockDataStream` |
| Validate output schemas | Internal `_validate_bar_df` / `_validate_quote_df` helpers |
| Emit structured logs | Python `logging` module with a JSON formatter |

### What it explicitly does NOT do

- Place, modify, or cancel orders (hard enforced — no order client is ever imported).
- Connect to the live trading API (`api.alpaca.markets` or `paper-api.alpaca.markets`
  for REST data).
- Persist data to disk.
- Compute any indicators or signals.

### Key research findings

- **REST client**: `alpaca-py` exposes `StockHistoricalDataClient` for historical
  bars. The client accepts `api_key` and `secret_key` at construction and defaults
  to `https://data.alpaca.markets`. Pagination is handled automatically by the SDK
  when using `.df` on the response object.
  ([alpaca-py SDK docs](https://alpaca.markets/sdks/python/market_data.html))

- **WebSocket client**: `StockDataStream` (from `alpaca.data.live`) manages the
  WebSocket lifecycle. Handlers are async functions registered with
  `subscribe_quotes(handler, *symbols)`. Unsubscription uses
  `unsubscribe_quotes(*symbols)`. The SDK handles reconnection internally, though
  we layer our own back-off logging on top.
  ([alpaca-py live data docs](https://alpaca.markets/sdks/python/market_data.html))

- **TimeFrame mapping**: The SDK uses `TimeFrame(amount, unit)` where `unit` is a
  `TimeFrameUnit` enum (`Minute`, `Hour`, `Day`). The config strings `"1m"`, `"5m"`,
  etc. must be mapped to these SDK objects.
  ([TimeFrame reference](https://alpaca.markets/sdks/python/api_reference/data/timeframe.html))

- **Feed**: Free-tier accounts should use `feed="iex"`. The WebSocket data stream
  endpoint for IEX is `wss://stream.data.alpaca.markets/v2/iex`.

---

## Architecture

### Module position in the pipeline

```
┌──────────────────────────────────────────────────────────┐
│                      Logging Module                       │
│  (observes all stages — JSON log events emitted by each) │
└──────────────────────────────────────────────────────────┘
          │                │               │              │
          ▼                ▼               ▼              ▼
┌──────────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────┐
│ data_ingestion│→│   strategy   │→│risk_engine│→│execution │
│   (this doc) │  │              │  │           │  │          │
└──────────────┘  └──────────────┘  └───────────┘  └──────────┘
```

`data_ingestion` outputs **only** pandas DataFrames to `strategy`. It never talks
to `strategy` directly; the pipeline orchestrator (not in scope here) routes the
data. This module has no knowledge of downstream modules.

### Internal architecture

```
data_ingestion/
├── __init__.py          # Public API surface (DataIngestionModule class)
├── config.yaml          # Runtime configuration
├── config_loader.py     # Reads and validates config.yaml
├── credentials.py       # Loads and validates .env credentials
├── rest_client.py       # Historical bar fetching (StockHistoricalDataClient)
├── stream_client.py     # Live quote streaming (StockDataStream)
├── schemas.py           # DataFrame schema constants and validation helpers
├── logger.py            # JSON structured logging setup
└── timeframe_map.py     # Maps config strings to alpaca-py TimeFrame objects
```

### Component interaction diagram

```
┌────────────────────────────────────────────────────────┐
│                  DataIngestionModule                    │
│  (__init__.py — the only class callers interact with)  │
│                                                        │
│  initialise()  get_historical_bars()  subscribe()      │
│                                unsubscribe() disconnect()│
└───┬──────────────────┬───────────────────┬─────────────┘
    │                  │                   │
    ▼                  ▼                   ▼
┌──────────┐   ┌──────────────┐   ┌──────────────────┐
│credentials│  │  RestClient  │   │  StreamClient    │
│  .py      │  │ rest_client  │   │  stream_client   │
└──────────┘   │     .py      │   │      .py         │
               └──────┬───────┘   └────────┬─────────┘
                      │                    │
               ┌──────▼───────┐   ┌────────▼─────────┐
               │ Alpaca REST  │   │ Alpaca WebSocket  │
               │  (HTTPS)     │   │  (WSS)           │
               └──────────────┘   └──────────────────┘
    │
    ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│config_loader │   │  schemas.py  │   │  logger.py   │
│    .py       │   │(validation)  │   │(JSON output) │
└──────────────┘   └──────────────┘   └──────────────┘
```

---

## Components and Interfaces

### 2.1 `DataIngestionModule` (public API)

This is the **only** class that code outside `data_ingestion/` should instantiate.
It owns a `RestClient` and a `StreamClient` and delegates to them.

```python
class DataIngestionModule:
    """
    Public entry point for the data_ingestion module.

    Typical usage:
        module = DataIngestionModule()
        module.initialise()
        df = module.get_historical_bars("AAPL", "1d", "2024-01-01", "2024-06-01")
        module.subscribe(["AAPL", "SPY"], my_callback)
        ...
        module.disconnect()
    """

    def initialise(self) -> None:
        """
        Load credentials and config, then build the REST and stream clients.
        Must be called before any other method.

        Raises:
            EnvironmentError: If .env is missing or credentials are absent/empty.
            FileNotFoundError: If config.yaml is missing.
            ValueError: If config.yaml is malformed or contains invalid values.
        """

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candle bars for one symbol.

        Args:
            symbol:    Ticker string, e.g. "AAPL".
            timeframe: One of "1m", "5m", "15m", "30m", "1h", "4h", "1d".
            start:     ISO 8601 date string, e.g. "2024-01-01".
            end:       ISO 8601 date string, strictly after start.

        Returns:
            DataFrame with UTC DatetimeIndex and columns:
            open (float64), high (float64), low (float64),
            close (float64), volume (int64).
            Returns an empty DataFrame with the same schema if no bars exist.

        Raises:
            ValueError: If any argument is invalid.
            IOError:    If the Alpaca API returns a non-2xx response.
        """

    def subscribe(
        self,
        symbols: list[str],
        callback: Callable[[pd.DataFrame], None],
    ) -> None:
        """
        Begin streaming live quotes for the given symbols.
        The callback is invoked in a background thread for each incoming quote.

        Args:
            symbols:  List of ticker strings to subscribe to.
            callback: Function receiving a single-row quote DataFrame.

        Raises:
            ConnectionError: If the WebSocket cannot connect.
            PermissionError: If Alpaca rejects the API credentials.
        """

    def unsubscribe(self, symbols: list[str]) -> None:
        """
        Stop quote callbacks for the given symbols.
        Silently ignores symbols that were not subscribed.
        Does not close the underlying connection.
        """

    def disconnect(self) -> None:
        """
        Close the WebSocket connection cleanly and stop all callbacks.
        Safe to call even if subscribe() was never called.
        """
```

---

### 2.2 `credentials.py`

Responsible for loading the two Alpaca keys from `.env`. No other module touches
the credentials file.

```python
def load_credentials() -> tuple[str, str]:
    """
    Load APCA_API_KEY_ID and APCA_API_SECRET_KEY from the .env file.

    Returns:
        (api_key, secret_key) — both guaranteed non-empty strings.

    Raises:
        EnvironmentError: If the .env file is missing, or if either variable
                          is absent or resolves to an empty string.
                          The message names the specific missing variable.

    Security note:
        The credential VALUES are never logged or printed anywhere in this
        module. Only the variable NAMES appear in error messages.
    """
```

---

### 2.3 `config_loader.py`

Reads `data_ingestion/config.yaml` and validates all fields. Returns a typed
`Config` dataclass so the rest of the module never has to deal with raw dicts.

```python
@dataclass
class Config:
    symbols: list[str]       # e.g. ["AAPL", "SPY"]
    timeframe: str           # e.g. "1d"
    lookback_days: int       # e.g. 90

def load_config(config_path: str) -> Config:
    """
    Read and validate data_ingestion/config.yaml.

    Args:
        config_path: Absolute or relative path to config.yaml.

    Returns:
        Validated Config dataclass instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError:        If YAML is invalid, required keys are missing,
                           or values fail validation rules.
    """
```

---

### 2.4 `timeframe_map.py`

Converts the human-readable config string (e.g. `"15m"`) to the `TimeFrame`
object that `alpaca-py` requires.

```python
# This dict maps the config string to the SDK's TimeFrame constructor args.
# TimeFrame(amount, unit) is the alpaca-py constructor.
TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1m":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5m":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15m": TimeFrame(15, TimeFrameUnit.Minute),
    "30m": TimeFrame(30, TimeFrameUnit.Minute),
    "1h":  TimeFrame(1,  TimeFrameUnit.Hour),
    "4h":  TimeFrame(4,  TimeFrameUnit.Hour),
    "1d":  TimeFrame(1,  TimeFrameUnit.Day),
}

VALID_TIMEFRAMES: frozenset[str] = frozenset(TIMEFRAME_MAP.keys())

def to_alpaca_timeframe(timeframe_str: str) -> TimeFrame:
    """
    Convert a config-file timeframe string to an alpaca-py TimeFrame.

    Args:
        timeframe_str: One of "1m", "5m", "15m", "30m", "1h", "4h", "1d".

    Returns:
        The corresponding alpaca-py TimeFrame object.

    Raises:
        ValueError: If timeframe_str is not in VALID_TIMEFRAMES.
    """
```

---

### 2.5 `schemas.py`

Defines the canonical DataFrame schemas and provides validation functions used
by both `RestClient` and `StreamClient` before any DataFrame leaves the module.

```python
# --- Bar schema ---
BAR_COLUMNS: dict[str, str] = {
    "open":   "float64",
    "high":   "float64",
    "low":    "float64",
    "close":  "float64",
    "volume": "int64",
}
# The index must be a DatetimeIndex with UTC timezone.

# --- Quote schema ---
QUOTE_COLUMNS: dict[str, str] = {
    "symbol":    "object",   # str stored as object dtype in pandas
    "bid_price": "float64",
    "ask_price": "float64",
    "mid_price": "float64",
    "timestamp": "datetime64[ns, UTC]",
}

def validate_bar_dataframe(df: pd.DataFrame) -> None:
    """
    Assert that df conforms to the bar schema.

    Raises:
        ValueError: Describing the first mismatch found (column name,
                    expected dtype, actual dtype).
    """

def validate_quote_dataframe(df: pd.DataFrame) -> None:
    """
    Assert that df conforms to the quote schema.

    Raises:
        ValueError: Describing the first mismatch found.
    """

def make_empty_bar_dataframe() -> pd.DataFrame:
    """
    Return a zero-row DataFrame with the correct bar schema.
    Used when an API call returns no data for the date range.
    """
```

---

### 2.6 `rest_client.py`

Wraps `StockHistoricalDataClient` and translates its response into a validated
pandas DataFrame.

```python
class RestClient:
    """
    Fetches historical OHLCV bar data from the Alpaca Market Data REST API.

    This class ONLY uses StockHistoricalDataClient (market data).
    It does NOT import or use TradingClient or BrokerClient.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Construct the underlying StockHistoricalDataClient.

        Note: StockHistoricalDataClient connects to data.alpaca.markets by
        default — NOT to paper-api.alpaca.markets. This is the correct
        endpoint for market data and enforces read-only behaviour.
        """

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """
        Fetch all historical bars for the given symbol and date range.

        The SDK's .df property handles pagination automatically — all pages
        are concatenated into a single DataFrame before this method returns.

        Args:
            symbol:    Non-empty ticker string.
            timeframe: Config string, e.g. "1d".
            start:     ISO 8601 date string.
            end:       ISO 8601 date string, strictly after start.

        Returns:
            Validated bar DataFrame (may be empty but never schema-incorrect).

        Raises:
            ValueError: If arguments are invalid.
            IOError:    If the Alpaca API returns a non-2xx status code.
        """
```

---

### 2.7 `stream_client.py`

Wraps `StockDataStream` and manages subscription state, reconnection logging,
and message parsing into the quote DataFrame schema.

```python
class StreamClient:
    """
    Streams live quote data from the Alpaca WebSocket feed.

    Uses StockDataStream from alpaca.data.live. The SDK manages the
    WebSocket connection lifecycle. This class adds:
      - subscription state tracking (which symbols are active)
      - structured logging on reconnect and malformed messages
      - DataFrame construction and schema validation for each quote
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Build the StockDataStream. Does NOT open the connection yet.
        Connection is opened on the first subscribe() call.
        """

    def subscribe(
        self,
        symbols: list[str],
        callback: Callable[[pd.DataFrame], None],
    ) -> None:
        """
        Subscribe to live quotes for the given symbols.
        Starts the stream's background event loop if not already running.
        """

    def unsubscribe(self, symbols: list[str]) -> None:
        """
        Unsubscribe from quotes for the given symbols.
        Symbols not in the current subscription are silently skipped.
        """

    def disconnect(self) -> None:
        """Stop the stream and close the WebSocket cleanly."""

    async def _handle_quote(self, data) -> None:
        """
        Internal async handler registered with StockDataStream.
        Called by the SDK for every incoming quote message.

        On valid data:   builds a single-row DataFrame, validates schema,
                         invokes the user callback.
        On invalid data: logs a WARNING with the raw message; does not
                         call the callback; does not raise.
        """
```

---

### 2.8 `logger.py`

Sets up a module-level Python logger that formats records as single-line JSON
objects. All other components in `data_ingestion/` obtain their logger via
`get_logger()`.

```python
def get_logger(name: str = "data_ingestion") -> logging.Logger:
    """
    Return a logger configured with a JSON formatter.

    The formatter emits records as:
    {"event": "...", "level": "INFO", "timestamp": "2024-01-01T00:00:00Z", ...}

    This format is consumed by the logging module downstream.
    """

def log_structured(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields,
) -> None:
    """
    Convenience wrapper that emits a structured JSON log record.

    Args:
        logger: The Logger instance to use.
        level:  logging.INFO, logging.DEBUG, logging.WARNING, etc.
        event:  The string value for the "event" field.
        **fields: Additional key-value pairs to include in the JSON record.

    Example:
        log_structured(logger, logging.INFO, "module_initialised",
                       symbols=["AAPL"], timeframe="1d")
        # emits: {"event": "module_initialised", "level": "INFO",
        #         "timestamp": "...", "symbols": ["AAPL"], "timeframe": "1d"}
    """
```

---

## Data Models

### 3.1 Bar DataFrame (historical candles)

| Field | dtype | Notes |
|---|---|---|
| index | `DatetimeIndex` | Timezone-aware UTC. Each row is the open time of that bar. |
| `open` | `float64` | Opening price of the bar. |
| `high` | `float64` | Highest price during the bar. |
| `low` | `float64` | Lowest price during the bar. |
| `close` | `float64` | Closing price of the bar. |
| `volume` | `int64` | Total shares traded during the bar. |

Example (2 rows, 1-day bars for AAPL):

```
                           open    high     low   close   volume
timestamp
2024-01-02 05:00:00+00:00  185.23  185.88  182.75  185.52  65234100
2024-01-03 05:00:00+00:00  183.41  184.12  181.90  184.25  58012000
```

### 3.2 Quote DataFrame (live WebSocket)

| Field | dtype | Notes |
|---|---|---|
| `symbol` | `object` (str) | Ticker string, e.g. `"AAPL"`. |
| `bid_price` | `float64` | Current best bid. |
| `ask_price` | `float64` | Current best ask. |
| `mid_price` | `float64` | `(bid_price + ask_price) / 2`, computed in-module. |
| `timestamp` | `datetime64[ns, UTC]` | Exchange timestamp of the quote. |

The quote DataFrame always has exactly **one row**. The downstream strategy
module appends rows to its own buffer; this module does not accumulate state.

### 3.3 Config dataclass

```python
@dataclass
class Config:
    symbols: list[str]   # Validated: each 1–10 chars, non-empty
    timeframe: str       # Validated: one of VALID_TIMEFRAMES
    lookback_days: int   # Validated: integer in [1, 365]
```

### 3.4 `config.yaml` file format

```yaml
# data_ingestion/config.yaml
# ---------------------------------------------------------------------------
# symbols: List of equity tickers to subscribe to and fetch history for.
#          Each entry must be a non-empty string of 1 to 10 characters.
symbols:
  - AAPL
  - SPY

# timeframe: Candle interval for historical data fetches.
# Accepted values: "1m", "5m", "15m", "30m", "1h", "4h", "1d"
timeframe: "1d"

# lookback_days: How many calendar days of history to fetch on startup.
# Must be an integer between 1 and 365 inclusive.
lookback_days: 90
```

---

## Algorithms

### Algorithm A: Credential loading

```
1. Call dotenv.load_dotenv() to read .env into the process environment.
2. For each required key in ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]:
   a. Read os.getenv(key).
   b. If the result is None or an empty string:
      raise EnvironmentError(f"Missing or empty credential: {key}")
3. Return (api_key, secret_key).
```

### Algorithm B: Config validation

```
1. If config.yaml does not exist: raise FileNotFoundError.
2. Open and parse config.yaml with yaml.safe_load().
   If parsing fails: raise ValueError with the YAML error message.
3. For each required key in ["symbols", "timeframe", "lookback_days"]:
   If key is absent from the parsed dict: collect it as missing.
   If any keys were missing: raise ValueError listing them all.
4. Validate symbols list:
   For i, symbol in enumerate(config["symbols"]):
     If not isinstance(symbol, str) or len(symbol) == 0 or len(symbol) > 10:
       raise ValueError(f"Invalid symbol at index {i}: '{symbol}'")
5. Validate timeframe:
   If config["timeframe"] not in VALID_TIMEFRAMES:
     raise ValueError listing the invalid value and all accepted values.
6. Validate lookback_days:
   If not isinstance(value, int) or value < 1 or value > 365:
     raise ValueError with the value and accepted range.
7. Return Config(symbols=..., timeframe=..., lookback_days=...).
```

### Algorithm C: Historical bar fetch

```
1. Validate inputs:
   - symbol must be a non-empty string.
   - timeframe must be in VALID_TIMEFRAMES.
   - start and end must parse as ISO 8601 date strings.
   - start must be strictly before end.
   If any check fails: raise ValueError naming the invalid argument.

2. Map timeframe string to alpaca-py TimeFrame via TIMEFRAME_MAP.

3. Build a StockBarsRequest:
   request = StockBarsRequest(
       symbol_or_symbols=symbol,
       timeframe=alpaca_timeframe,
       start=datetime.fromisoformat(start),
       end=datetime.fromisoformat(end),
       feed="iex",    # free-tier compatible
   )

4. Call self._client.get_stock_bars(request).
   If the SDK raises an APIError: extract status_code and body,
   raise IOError(f"Alpaca API error {status_code}: {body}").

5. Convert response to DataFrame: df = response.df
   This step collapses all pages into one DataFrame.

6. If df is empty: return make_empty_bar_dataframe().

7. Normalise the DataFrame:
   - Keep only columns: open, high, low, close, volume.
   - Cast price columns to float64, volume to int64.
   - Ensure index is UTC-aware DatetimeIndex.

8. Validate schema: call validate_bar_dataframe(df).

9. Emit DEBUG log event "historical_fetch_complete" with bar_count.

10. Return df.
```

### Algorithm D: WebSocket quote handling

```
On subscribe(symbols, callback):
1. Store callback reference.
2. Register async handler _handle_quote with self._stream.subscribe_quotes().
3. If stream is not running: start it in a background thread.
4. Update self._subscribed_symbols set.

On _handle_quote(data):
1. Try to extract: symbol, bid_price, ask_price, timestamp from data.
   If any field is missing or wrong type:
     log WARNING "malformed_quote" with repr(data); return immediately.

2. Compute mid_price = (bid_price + ask_price) / 2.

3. Build DataFrame:
   df = pd.DataFrame([{
       "symbol": symbol,
       "bid_price": bid_price,
       "ask_price": ask_price,
       "mid_price": mid_price,
       "timestamp": pd.Timestamp(timestamp, tz="UTC"),
   }])
   Cast dtypes to match quote schema.

4. Validate schema: call validate_quote_dataframe(df).
   If validation fails: log WARNING with the mismatch; return.

5. Invoke callback(df).

On unsubscribe(symbols):
1. For each symbol:
   If symbol not in self._subscribed_symbols: skip (silent ignore).
   Else: call self._stream.unsubscribe_quotes(symbol) and remove from set.

On disconnect():
1. Call self._stream.stop().
2. Clear self._subscribed_symbols.
```

### Algorithm E: Exponential back-off (reconnect logging)

The `StockDataStream` SDK handles reconnection internally. The `StreamClient`
hooks into the SDK's on-connect callback to log the reconnect event with our
structured format and to track the attempt number.

```
back_off_delay(attempt: int) -> float:
    # attempt starts at 1 (first reconnect attempt)
    # delay doubles each attempt, capped at 60 seconds
    base = 1.0         # seconds
    cap  = 60.0        # seconds
    return min(base * (2 ** (attempt - 1)), cap)

# attempt=1 → 1s, attempt=2 → 2s, attempt=3 → 4s, ..., attempt=7+ → 60s
```

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of a system — essentially, a formal statement about what the system should
do. Properties serve as the bridge between human-readable specifications and
machine-verifiable correctness guarantees.*

### Property 1: Historical bar output schema is always correct

*For any* valid combination of symbol (non-empty string), timeframe (one of the
seven accepted values), and date range (start strictly before end), when
`get_historical_bars` is called against a mocked Alpaca client that returns a
non-empty bar response, the returned DataFrame SHALL have a UTC-aware
`DatetimeIndex` and columns `open` (float64), `high` (float64), `low` (float64),
`close` (float64), `volume` (int64).

**Validates: Requirements 2.1, 5.1, 5.3**

---

### Property 2: Non-2xx HTTP responses always raise IOError

*For any* HTTP status code in the range 400–599, when the Alpaca REST API returns
that status code, `get_historical_bars` SHALL raise an `IOError` whose message
contains that status code.

**Validates: Requirements 2.3**

---

### Property 3: Paginated responses are fully aggregated

*For any* sequence of N pages each containing K bars, when the Alpaca API returns
those pages via pagination, `get_historical_bars` SHALL return a single DataFrame
containing exactly N × K rows (for equal-sized pages) or the total across all
pages (for varying sizes), with no bars dropped or duplicated.

**Validates: Requirements 2.5**

---

### Property 4: Invalid inputs always raise ValueError with the argument name

*For any* invalid input (empty symbol, unsupported timeframe string, or start ≥
end date pair), calling `get_historical_bars` SHALL raise a `ValueError` whose
message contains the name of the offending argument.

**Validates: Requirements 2.6**

---

### Property 5: Quote callback always receives a correctly-typed DataFrame

*For any* incoming quote message with arbitrary symbol string and non-negative
bid/ask float values, the internal quote handler SHALL invoke the user callback
with a single-row DataFrame that: (a) has exactly the columns defined in the
quote schema, (b) has the correct dtypes, and (c) has `mid_price` equal to
`(bid_price + ask_price) / 2`.

**Validates: Requirements 3.2, 5.2, 5.3**

---

### Property 6: Exponential back-off is always bounded

*For any* reconnect attempt number N (starting at 1), the computed back-off
delay SHALL satisfy: `delay >= 1.0` and `delay <= 60.0` and
`delay == min(2^(N-1), 60.0)`.

**Validates: Requirements 3.3**

---

### Property 7: Malformed quote messages never invoke the callback or raise

*For any* message object that is missing required fields or has incorrectly typed
fields, the internal quote handler SHALL NOT call the user callback and SHALL NOT
raise any exception; it SHALL log exactly one WARNING event.

**Validates: Requirements 3.7**

---

### Property 8: Unsubscribe is safe for any symbol set

*For any* set of currently-subscribed symbols S and any set of symbols U to
unsubscribe (where U may include symbols not in S), calling
`unsubscribe(U)` SHALL: (a) stop callbacks for symbols in S ∩ U, and (b) silently
ignore symbols in U \ S without raising any exception.

**Validates: Requirements 3.5**

---

### Property 9: Schema validation catches any dtype mismatch

*For any* DataFrame produced internally that has exactly one column with the
wrong dtype (or a missing column, or a non-UTC index), `validate_bar_dataframe`
or `validate_quote_dataframe` SHALL raise a `ValueError` whose message identifies
the first mismatch by column name, expected dtype, and actual dtype.

**Validates: Requirements 5.3**

---

### Property 10: Bar DataFrame JSON round-trip preserves all data

*For any* valid historical bar DataFrame (arbitrary number of rows, arbitrary
OHLCV values, arbitrary UTC timestamps), serialising to JSON with
`df.to_json(orient="split", date_format="iso")` and deserialising back SHALL
produce a DataFrame equal to the original in column names, dtypes, index values,
and cell values.

**Validates: Requirements 5.4**

---

### Property 11: Symbol validation rejects exactly invalid entries

*For any* list of symbols where at least one entry is empty or longer than 10
characters, `load_config` SHALL raise a `ValueError` identifying the offending
entry and its position. *For any* list where all entries are non-empty strings of
1–10 characters, `load_config` SHALL not raise.

**Validates: Requirements 6.3**

---

### Property 12: Timeframe validation accepts only the seven valid strings

*For any* string value for `timeframe` in `config.yaml`, `load_config` SHALL
raise a `ValueError` if and only if the value is not in
`{"1m", "5m", "15m", "30m", "1h", "4h", "1d"}`.

**Validates: Requirements 6.4**

---

### Property 13: lookback_days validation enforces the [1, 365] range

*For any* integer value for `lookback_days`, `load_config` SHALL raise a
`ValueError` if and only if the value is outside the closed interval [1, 365].
For any non-integer value, it SHALL also raise.

**Validates: Requirements 6.5**

---

### Property 14: Structured DEBUG log always contains required fields after a successful fetch

*For any* valid historical bar fetch (arbitrary symbol, timeframe, date range),
the emitted DEBUG log event SHALL contain the fields `event`, `level`, `timestamp`,
`symbol`, `timeframe`, `start`, `end`, and `bar_count`, with `bar_count` equal to
the number of rows in the returned DataFrame.

**Validates: Requirements 7.2**

---

### Property 15: Reconnect WARNING log always contains correct backoff_seconds

*For any* reconnect attempt number N, the emitted WARNING log event SHALL contain
`attempt` equal to N and `backoff_seconds` equal to `min(2^(N-1), 60.0)`.

**Validates: Requirements 7.3**

---

## Error Handling

### Error catalogue

| Error class | When raised | Who raises it | What to include in message |
|---|---|---|---|
| `EnvironmentError` | Missing/empty credential | `credentials.py` | Name of the missing key |
| `FileNotFoundError` | `config.yaml` absent | `config_loader.py` | Expected file path |
| `ValueError` | YAML syntax error | `config_loader.py` | YAML parser error text |
| `ValueError` | Missing required config key | `config_loader.py` | Names of all missing keys |
| `ValueError` | Invalid symbol in list | `config_loader.py` | Entry value and list index |
| `ValueError` | Invalid timeframe | `config_loader.py` / `rest_client.py` | Invalid value + accepted values |
| `ValueError` | `lookback_days` out of range | `config_loader.py` | Value and accepted range |
| `ValueError` | Invalid `get_historical_bars` args | `rest_client.py` | Argument name |
| `ValueError` | DataFrame schema mismatch | `schemas.py` | Column, expected dtype, actual dtype |
| `IOError` | Non-2xx from Alpaca REST | `rest_client.py` | HTTP status code + response body |
| `ConnectionError` | WebSocket cannot connect | `stream_client.py` | Descriptive message |
| `PermissionError` | Alpaca rejects credentials on WS | `stream_client.py` | Descriptive message |
| `RuntimeError` | Order API call attempted | `__init__.py` guard | "Module is read-only" message |

### Error propagation policy

1. **Initialisation errors** (`EnvironmentError`, `FileNotFoundError`, `ValueError`
   from `initialise()`) propagate directly to the caller. The module is unusable
   until these are resolved.

2. **Per-request errors** (`IOError`, `ValueError` from `get_historical_bars`)
   propagate to the caller after the error log event is emitted (Requirement 7.5).

3. **Stream errors** (`ConnectionError`, `PermissionError`) propagate to the caller
   of `subscribe()`. Auth failures are not retried.

4. **Malformed messages** on the WebSocket are swallowed (WARNING only, no
   exception), so a bad tick does not crash the stream (Requirement 3.7).

5. **Any unhandled exception** that escapes the module triggers the structured
   ERROR log (Requirement 7.4) before re-raising.

### Read-only enforcement

The `data_ingestion` module enforces read-only access at three levels:

1. **Import level**: The module never imports `TradingClient`, `BrokerClient`,
   or any class from `alpaca.trading`. If such an import were accidentally added,
   it would be caught by an import inspection smoke test.

2. **Client level**: `RestClient` uses only `StockHistoricalDataClient`
   (market data endpoint). `StreamClient` uses only `StockDataStream`. Neither
   client exposes order methods.

3. **Guard level**: A `_no_order_operations()` sentinel function is defined in
   `__init__.py`. If any future code path were to call an order function, it
   raises `RuntimeError("data_ingestion is read-only: order operations are prohibited")`.

---

## Testing Strategy

### Overview

Two complementary test layers are used:

- **Property-based tests** (via [Hypothesis](https://hypothesis.readthedocs.io/))
  verify universal properties across many generated inputs. They are configured
  to run at least 100 iterations each.
- **Unit/example tests** (via `pytest`) cover specific scenarios, edge cases, and
  error conditions that do not benefit from input generation.

All Alpaca API calls are mocked in unit and property tests. No real network calls
are made during testing.

### Property-based test configuration

Install Hypothesis alongside `pytest`:

```
pytest
hypothesis
```

Each property test is tagged with a reference to its design document property:

```python
# Feature: data-ingestion, Property 1: Historical bar output schema is always correct
@given(...)
@settings(max_examples=100)
def test_bar_schema_always_correct(...):
    ...
```

### Test file structure

```
tests/
└── data_ingestion/
    ├── test_credentials.py       # Smoke + edge cases for credential loading
    ├── test_config_loader.py     # Smoke + edge cases + Properties 11, 12, 13
    ├── test_rest_client.py       # Properties 1, 2, 3, 4 + edge cases
    ├── test_stream_client.py     # Properties 5, 6, 7, 8 + edge cases
    ├── test_schemas.py           # Properties 9, 10
    ├── test_logger.py            # Properties 14, 15 + example tests
    └── test_read_only.py         # Smoke tests for import-level enforcement
```

### Example test cases (non-property)

| Test | Category | Covers |
|---|---|---|
| Valid `.env` loads both keys | SMOKE | Req 1.1 |
| Missing `.env` raises `EnvironmentError` | EDGE_CASE | Req 1.2 |
| Empty key raises `EnvironmentError` naming the key | EDGE_CASE | Req 1.2 |
| Credentials never appear in log output | EXAMPLE | Req 1.3 |
| `RestClient` uses `data.alpaca.markets`, not `paper-api` | SMOKE | Req 2.2 |
| Zero-bar response returns empty DataFrame with correct schema | EDGE_CASE | Req 2.4 |
| `subscribe()` calls `subscribe_quotes` on the stream | INTEGRATION | Req 3.1 |
| Auth rejection raises `PermissionError`, no reconnect | EDGE_CASE | Req 3.4 |
| `disconnect()` calls `stream.stop()` | EXAMPLE | Req 3.6 |
| `TradingClient` is not imported anywhere in `data_ingestion/` | SMOKE | Req 4.1, 4.2 |
| Order guard raises `RuntimeError` | EXAMPLE | Req 4.3 |
| `module_initialised` INFO log contains all required fields | EXAMPLE | Req 7.1 |
| Unhandled exception emits ERROR log then re-raises | EDGE_CASE | Req 7.4 |
| `config.yaml` absent raises `FileNotFoundError` | EDGE_CASE | Req 6.2 |
| Invalid YAML raises `ValueError` with parse error | EDGE_CASE | Req 6.2 |
| Missing required config key raises `ValueError` naming keys | EDGE_CASE | Req 6.2 |

### Property test summary

| Test | Hypothesis strategy | Property |
|---|---|---|
| Bar schema always correct | `st.from_regex(r'[A-Z]{1,5}')` for symbols, `st.sampled_from(VALID_TIMEFRAMES)` for timeframes, random date pairs | Property 1 |
| Non-2xx raises IOError | `st.integers(400, 599)` for status codes | Property 2 |
| Pagination aggregates all bars | `st.lists(st.lists(st.fixed_dictionaries({OHLCV fields})))` | Property 3 |
| Invalid args raise ValueError | `st.one_of(st.just(""), st.text(), st.dates())` injected as bad args | Property 4 |
| Quote callback gets correct DataFrame | `st.text(min_size=1, max_size=5)` symbol, `st.floats(0, 1000)` bid/ask | Property 5 |
| Back-off always bounded | `st.integers(1, 100)` for attempt number | Property 6 |
| Malformed messages silent | `st.one_of(st.none(), st.integers(), st.text(), st.fixed_dictionaries({...missing fields}))` | Property 7 |
| Unsubscribe safe for any symbol set | `st.sets(st.text(min_size=1, max_size=5))` for subscribed and unsubscribe sets | Property 8 |
| Schema validation catches mismatches | Generate DataFrames with one injected violation | Property 9 |
| JSON round-trip preserves data | `st.data_frames(...)` with OHLCV columns and UTC timestamps | Property 10 |
| Symbol validation rejects invalid | `st.lists(st.one_of(valid_symbols, invalid_symbols))` | Property 11 |
| Timeframe validation exact | `st.text()` including and excluding valid values | Property 12 |
| lookback_days range enforcement | `st.integers(-1000, 1000)` | Property 13 |
| DEBUG log fields always present | Random valid fetch params | Property 14 |
| WARNING log backoff_seconds correct | `st.integers(1, 50)` for attempt | Property 15 |
