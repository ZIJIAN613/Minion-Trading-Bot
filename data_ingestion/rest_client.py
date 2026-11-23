# data_ingestion/rest_client.py
#
# Fetches historical OHLCV candle bars from the Alpaca Market Data REST API.
#
# ============================================================
# READ-ONLY ENFORCEMENT — CRITICAL SAFETY CONSTRAINT
# ============================================================
# This file MUST NEVER import TradingClient or BrokerClient.
# Those classes expose order-placement methods that are strictly
# prohibited in the data_ingestion module (Requirement 4.1, 4.2).
#
# Allowed import: StockHistoricalDataClient (market data only)
#   from alpaca.data.historical
#
# Forbidden imports (must never appear anywhere in this file):
#   from alpaca.trading.client import TradingClient      ← FORBIDDEN
#   from alpaca.broker.client import BrokerClient        ← FORBIDDEN
#   import alpaca.trading                                ← FORBIDDEN
#   import alpaca.broker                                 ← FORBIDDEN
#
# Enforcement: the static AST scan in tests/test_read_only.py
# inspects all .py source files under data_ingestion/ and fails
# if any forbidden symbol is detected in import statements.
# ============================================================
#
# Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 4.1, 4.2, 7.2, 7.5

import logging
from datetime import datetime

import pandas as pd

# StockHistoricalDataClient: the ONLY Alpaca client permitted here.
# It connects to https://data.alpaca.markets — the market data endpoint —
# NOT to https://paper-api.alpaca.markets (the trading endpoint).
# This is the correct, read-only endpoint for historical bar data.
from alpaca.data.historical import StockHistoricalDataClient

# StockBarsRequest carries the query parameters for a bars request.
from alpaca.data.requests import StockBarsRequest

# APIError is raised by the SDK when the server returns a non-2xx status.
from alpaca.common.exceptions import APIError

# Import helpers from sibling modules within the data_ingestion package.
# The dot prefix (from .schemas) is a *relative import* — Python's way of
# saying "look inside the current package". It is equivalent to
# #include "schemas.h" in a C++ project where schemas.h is in the same
# directory.
from data_ingestion.schemas import (
    validate_bar_dataframe,
    make_empty_bar_dataframe,
    BAR_COLUMNS,
)
from data_ingestion.timeframe_map import VALID_TIMEFRAMES, to_alpaca_timeframe
from data_ingestion.logger import get_logger, log_structured


# Module-level logger for this file.
# Using a hierarchical name ("data_ingestion.rest_client") follows Python
# logging conventions — it lets the root "data_ingestion" logger control
# the entire package's log level if needed, similar to how a C++ logging
# namespace hierarchy works.
_logger = get_logger("data_ingestion.rest_client")


# ---------------------------------------------------------------------------
# RestClient class
# ---------------------------------------------------------------------------

class RestClient:
    """
    Fetches historical OHLCV bar data from the Alpaca Market Data REST API.

    This class ONLY uses StockHistoricalDataClient (market data endpoint).
    It does NOT import or use TradingClient or BrokerClient — doing so would
    violate the read-only constraint of the data_ingestion module.

    Typical usage:
        client = RestClient(api_key, secret_key)
        df = client.get_historical_bars("AAPL", "1d", "2024-01-01", "2024-06-01")
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Construct the underlying StockHistoricalDataClient.

        StockHistoricalDataClient defaults to https://data.alpaca.markets —
        the Alpaca market data endpoint — NOT to paper-api.alpaca.markets or
        api.alpaca.markets. This is intentional: the market data endpoint is
        read-only by design, which enforces our safety requirement that no
        order can ever be placed from this module.

        Args:
            api_key:    Alpaca API key ID (APCA_API_KEY_ID from .env).
            secret_key: Alpaca API secret key (APCA_API_SECRET_KEY from .env).
        """
        # Instantiate the market-data-only client.
        # This does NOT open a network connection yet — the connection is made
        # lazily when the first request is sent (standard HTTP client behaviour).
        #
        # StockHistoricalDataClient(api_key, secret_key) sets:
        #   base_url = "https://data.alpaca.markets"  ← market data, read-only
        # Compare with TradingClient, which would use:
        #   base_url = "https://paper-api.alpaca.markets"  ← NEVER used here
        self._client = StockHistoricalDataClient(api_key, secret_key)

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """
        Fetch all historical OHLCV bars for a symbol over a date range.

        Follows Algorithm C from the design document exactly:
          1. Validate inputs
          2. Map timeframe string → SDK TimeFrame object
          3. Build StockBarsRequest with feed="iex"
          4. Call the SDK; handle APIError → IOError
          5. Convert response to DataFrame
          6. Return empty DataFrame if no bars
          7. Normalise dtypes and columns
          8. Validate schema
          9. Emit DEBUG log
         10. Return

        The SDK's .df property handles pagination automatically — all pages
        returned by the API are concatenated into a single DataFrame before
        this method returns (Requirement 2.5).

        Args:
            symbol:    Non-empty ticker string, e.g. "AAPL".
            timeframe: Config string — one of "1m", "5m", "15m", "30m",
                       "1h", "4h", "1d".
            start:     ISO 8601 date string, e.g. "2024-01-01".
            end:       ISO 8601 date string strictly after start.

        Returns:
            Validated bar DataFrame with UTC DatetimeIndex and columns:
            open (float64), high (float64), low (float64),
            close (float64), volume (int64).
            If no bars exist for the range, returns an empty DataFrame
            with the same schema (never returns None).

        Raises:
            ValueError: If any argument is invalid (empty symbol, unknown
                        timeframe, unparseable date, start >= end).
                        The message always names the offending argument.
            IOError:    If the Alpaca API returns a non-2xx HTTP status.
                        The message contains the status code and body.
        """

        # ------------------------------------------------------------------
        # Step 1: Validate inputs
        # ------------------------------------------------------------------

        # --- 1a. symbol must be a non-empty string ---
        # isinstance(x, str) checks the runtime type — like dynamic_cast in C++.
        # We also strip() to reject strings that are only whitespace.
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(
                f"Invalid argument 'symbol': must be a non-empty string, "
                f"got {repr(symbol)}"
            )

        # --- 1b. timeframe must be in VALID_TIMEFRAMES ---
        # VALID_TIMEFRAMES is a frozenset, so this check is O(1).
        if timeframe not in VALID_TIMEFRAMES:
            accepted = ", ".join(sorted(VALID_TIMEFRAMES))
            raise ValueError(
                f"Invalid argument 'timeframe': '{timeframe}' is not accepted. "
                f"Accepted values are: {accepted}"
            )

        # --- 1c. start must parse as an ISO 8601 date string ---
        # datetime.fromisoformat() is Python 3.7+ and accepts strings like
        # "2024-01-01" or "2024-01-01T00:00:00". It raises ValueError on
        # malformed input, which we catch and re-raise with a better message.
        try:
            start_dt = datetime.fromisoformat(start)
        except (ValueError, TypeError):
            raise ValueError(
                f"Invalid argument 'start': '{start}' is not a valid "
                f"ISO 8601 date string (expected format: YYYY-MM-DD)"
            )

        # --- 1d. end must parse as an ISO 8601 date string ---
        try:
            end_dt = datetime.fromisoformat(end)
        except (ValueError, TypeError):
            raise ValueError(
                f"Invalid argument 'end': '{end}' is not a valid "
                f"ISO 8601 date string (expected format: YYYY-MM-DD)"
            )

        # --- 1e. start must be strictly before end ---
        # Comparing datetime objects uses their natural ordering — same as
        # operator< on std::chrono::time_point in C++.
        if start_dt >= end_dt:
            raise ValueError(
                f"Invalid arguments: 'start' must be strictly before 'end', "
                f"but got start='{start}' >= end='{end}'"
            )

        # ------------------------------------------------------------------
        # Step 2: Map timeframe string to alpaca-py TimeFrame object
        # ------------------------------------------------------------------

        # to_alpaca_timeframe() does the dict lookup and raises ValueError on
        # unknown keys — but we already validated above, so this won't raise.
        alpaca_timeframe = to_alpaca_timeframe(timeframe)

        # ------------------------------------------------------------------
        # Step 3: Build StockBarsRequest
        # ------------------------------------------------------------------

        # StockBarsRequest is a Pydantic model (like a C++ struct with built-in
        # validation). We pass feed="iex" which is required for free-tier
        # Alpaca accounts — without it the request would use the SIP feed and
        # return an authorisation error on a free account.
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=alpaca_timeframe,
            start=start_dt,
            end=end_dt,
            feed="iex",    # Required for free-tier accounts (IEX feed)
        )

        # ------------------------------------------------------------------
        # Step 4: Call the SDK — handle APIError → IOError
        # ------------------------------------------------------------------

        # try/except is Python's equivalent of try/catch in C++.
        # We specifically catch alpaca.common.exceptions.APIError, which the
        # SDK raises when the server returns a non-2xx HTTP status code.
        try:
            response = self._client.get_stock_bars(request)
        except APIError as exc:
            # Extract the status code and body from the exception.
            # The alpaca-py APIError stores the HTTP status code in .status_code
            # and the response body in its string representation.
            status_code = getattr(exc, "status_code", "unknown")
            body = str(exc)

            # Emit ERROR log before re-raising (Requirement 7.5).
            # log_structured() is our structured JSON logger helper.
            log_structured(
                _logger,
                logging.ERROR,
                "historical_fetch_failed",
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                error_type=type(exc).__name__,
                error_message=body,
            )

            # Re-raise as IOError with the status code and body in the message.
            # IOError is Python's built-in I/O exception — appropriate here
            # because this is a network/HTTP failure, not a logic error.
            raise IOError(
                f"Alpaca API error {status_code}: {body}"
            ) from exc

        # ------------------------------------------------------------------
        # Step 5: Convert response to DataFrame
        # ------------------------------------------------------------------

        # response.df is a pandas DataFrame property provided by the alpaca-py
        # SDK. It automatically handles pagination — all pages are fetched and
        # concatenated before .df returns. This satisfies Requirement 2.5.
        #
        # The resulting DataFrame ALWAYS has a MultiIndex of (symbol, timestamp),
        # even for single-symbol requests. Step 7 below flattens this to a
        # plain DatetimeIndex before returning to the caller.
        df = response.df

        # ------------------------------------------------------------------
        # Step 6: Return empty DataFrame if no bars were returned
        # ------------------------------------------------------------------

        # df.empty is True when the DataFrame has no rows (but keeps its
        # column schema). We use make_empty_bar_dataframe() to return a
        # properly-typed empty frame instead of None — callers always get
        # a DataFrame, never None (Requirement 2.4).
        if df.empty:
            return make_empty_bar_dataframe()

        # ------------------------------------------------------------------
        # Step 7: Normalise the DataFrame
        # ------------------------------------------------------------------

        # The SDK may return extra columns (e.g., 'trade_count', 'vwap').
        # We keep only the canonical bar columns to ensure schema consistency.
        #
        # List comprehension: [x for x in iterable if condition] is Python's
        # compact way to build a filtered list. This line is equivalent to:
        #   available = []
        #   for col in BAR_COLUMNS:
        #       if col in df.columns:
        #           available.append(col)
        available_cols = [col for col in BAR_COLUMNS if col in df.columns]
        df = df[available_cols]

        # Cast price columns to float64 and volume to int64.
        # .astype(dict) applies per-column dtype casts in one call — like a
        # batch reinterpret_cast across all columns simultaneously.
        # We only cast columns that are present (available_cols already filtered).
        cast_map = {col: dtype for col, dtype in BAR_COLUMNS.items() if col in df.columns}
        df = df.astype(cast_map)

        # Ensure the index is a flat UTC-aware DatetimeIndex.
        #
        # The alpaca-py SDK's .df property ALWAYS returns a MultiIndex of
        # (symbol, timestamp) — even for single-symbol requests. We must
        # flatten it to a plain DatetimeIndex before doing any UTC work.
        # If a future SDK version changes this, the isinstance check below
        # handles both cases safely.
        #
        # MultiIndex.get_level_values("timestamp") extracts just the timestamp
        # level as a DatetimeIndex — like projecting one column out of a
        # composite key in a database index.
        if isinstance(df.index, pd.MultiIndex):
            # Extract the timestamp level. The SDK names it "timestamp".
            # If the level name is different (or positional), fall back to
            # the last level (level -1), which is always the timestamp in
            # alpaca-py's bar responses.
            if "timestamp" in df.index.names:
                df.index = df.index.get_level_values("timestamp")
            else:
                # Fallback: the timestamp is the innermost (last) level.
                df.index = df.index.get_level_values(-1)

        # Now df.index should be a DatetimeIndex (or convertible to one).
        # Normalise to UTC-aware.
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            # tz_localize("UTC") stamps a timezone-naive DatetimeIndex as UTC.
            df.index = df.index.tz_localize("UTC")
        elif str(df.index.tz) != "UTC":
            # tz_convert("UTC") converts from one timezone to UTC.
            df.index = df.index.tz_convert("UTC")

        # ------------------------------------------------------------------
        # Step 8: Validate schema
        # ------------------------------------------------------------------

        # validate_bar_dataframe() raises ValueError on the first schema
        # mismatch it finds, naming the column and dtypes involved.
        # This is the final quality gate before the DataFrame leaves this module.
        validate_bar_dataframe(df)

        # ------------------------------------------------------------------
        # Step 9: Emit DEBUG log
        # ------------------------------------------------------------------

        # log_structured() wraps logger.log() with structured JSON output.
        # We include all fields required by Requirement 7.2.
        log_structured(
            _logger,
            logging.DEBUG,
            "historical_fetch_complete",
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            bar_count=len(df),  # len(df) is the row count — like df.size() in C++
        )

        # ------------------------------------------------------------------
        # Step 10: Return
        # ------------------------------------------------------------------

        return df
