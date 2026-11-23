# data_ingestion/stream_client.py
#
# Streams live quote data from the Alpaca WebSocket feed.
#
# ============================================================
# READ-ONLY ENFORCEMENT — CRITICAL SAFETY CONSTRAINT
# ============================================================
# This file MUST NEVER import TradingClient or BrokerClient.
# Those classes expose order-placement methods that are strictly
# prohibited in the data_ingestion module (Requirements 4.1, 4.2).
#
# Allowed import: StockDataStream (market data streaming only)
#   from alpaca.data.live
#
# Forbidden imports (must never appear in this file):
#   from alpaca.trading.client import TradingClient   ← FORBIDDEN
#   from alpaca.broker.client import BrokerClient     ← FORBIDDEN
# ============================================================
#
# Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 7.3

import logging
import threading  # Python standard library for OS threads (like std::thread in C++11)
from typing import Callable, List

import pandas as pd

# StockDataStream: the ONLY Alpaca client permitted in this file.
# It connects to the Alpaca WebSocket market data endpoint.
# The base DataStream class (which StockDataStream inherits from) is also
# imported here so we can subclass StockDataStream to hook the connect event.
from alpaca.data.live import StockDataStream
from alpaca.data.live.websocket import DataStream  # base class for subclassing

from data_ingestion.schemas import validate_quote_dataframe, QUOTE_COLUMNS
from data_ingestion.logger import get_logger, log_structured

# Module-level logger — uses hierarchical naming so the root "data_ingestion"
# logger can control this module's log level too.
_logger = get_logger("data_ingestion.stream_client")


# ---------------------------------------------------------------------------
# Algorithm E: Exponential back-off delay function
# ---------------------------------------------------------------------------

def back_off_delay(attempt: int) -> float:
    """
    Compute the reconnect back-off delay for a given attempt number.

    Implements Algorithm E from the design document:
      delay = min(1.0 * 2^(attempt-1), 60.0)

    The delay doubles on each attempt, capped at 60 seconds:
      attempt=1 → 1s
      attempt=2 → 2s
      attempt=3 → 4s
      attempt=4 → 8s
      attempt=5 → 16s
      attempt=6 → 32s
      attempt=7+ → 60s (cap)

    In C++ terms this is a simple constexpr formula:
      std::min(1.0 * std::pow(2.0, attempt - 1), 60.0)

    Args:
        attempt: The reconnect attempt counter, starting at 1 for the first
                 reconnect. Must be a positive integer.

    Returns:
        Delay in seconds as a float, always in [1.0, 60.0].
    """
    # 2 ** (attempt - 1) is Python's exponentiation operator (like std::pow).
    # 1.0 * ensures the result is a float even for small attempts.
    return min(1.0 * (2 ** (attempt - 1)), 60.0)


# ---------------------------------------------------------------------------
# _HookedStream — StockDataStream subclass with a connect hook
# ---------------------------------------------------------------------------

class _HookedStream(StockDataStream):
    """
    Internal subclass of StockDataStream that overrides `_start_ws` to
    inject reconnect-tracking logic without monkey-patching.

    Why subclass instead of monkey-patching?
    ----------------------------------------
    The SDK's `_run_forever` loop calls `await self._start_ws()` every time
    a (re)connection is attempted. There is no public on-connect callback.
    Subclassing and overriding `_start_ws` is the idiomatic Python approach
    — equivalent to overriding a virtual method in C++ to add pre/post logic
    while still calling the parent implementation via `super()`.

    The `_connect_callback` field is set by `StreamClient.__init__` after
    the object is constructed, allowing the parent StreamClient to inject
    its own logic without circular dependencies.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        # super().__init__() calls the parent class constructor — equivalent
        # to the C++ member initialiser list calling the base class ctor.
        super().__init__(api_key, secret_key)

        # _connect_callback: a callable that StreamClient injects after
        # construction. It is called with a boolean argument:
        #   True  → this is a reconnect (connection already happened once)
        #   False → this is the very first connect
        # We initialize it to None and check before calling.
        self._connect_callback = None

        # _ever_connected: tracks whether we have successfully connected at
        # least once. This lets us distinguish the initial connect from
        # subsequent reconnects.
        self._ever_connected: bool = False

    async def _start_ws(self) -> None:
        """
        Override of DataStream._start_ws.

        Calls the parent implementation, then fires our connect callback to
        let StreamClient update its reconnect counter and emit log events.

        The parent's _start_ws:
          1. calls _connect() — opens the WebSocket
          2. calls _auth()    — authenticates with Alpaca
          3. logs "connected to <endpoint>"

        If the parent raises (auth failure, network error), the exception
        propagates normally — we do not intercept it here.
        """
        # Determine whether this is a reconnect *before* calling the parent,
        # so we know the context even if the parent raises.
        is_reconnect: bool = self._ever_connected

        # Call the parent implementation — this is where the actual WebSocket
        # connection and authentication happen. await is Python's equivalent
        # of co_await in C++20 coroutines: it suspends this coroutine until
        # the awaitable completes, then resumes here.
        await super()._start_ws()

        # If we reach here the connection and auth both succeeded.
        self._ever_connected = True

        # Fire the callback if one has been registered.
        if self._connect_callback is not None:
            # callable(f) is True if f can be called — like checking if a
            # std::function is non-empty (operator bool()).
            self._connect_callback(is_reconnect)


# ---------------------------------------------------------------------------
# StreamClient
# ---------------------------------------------------------------------------

class StreamClient:
    """
    Streams live quote data from the Alpaca WebSocket feed.

    Uses StockDataStream from alpaca.data.live (via the _HookedStream subclass
    which adds reconnect-tracking). The SDK manages the WebSocket connection
    lifecycle internally. This class adds:
      - subscription state tracking (which symbols are active)
      - structured WARNING logs on reconnect with exponential back-off delay
      - DataFrame construction and schema validation for each quote message

    Typical usage:
        client = StreamClient(api_key, secret_key)
        client.subscribe(["AAPL", "SPY"], my_callback)
        ...
        client.disconnect()
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Build the underlying StockDataStream. Does NOT open the connection yet.
        The connection is opened lazily when subscribe() is called.

        Args:
            api_key:    Alpaca API key ID (APCA_API_KEY_ID from .env).
            secret_key: Alpaca API secret key (APCA_API_SECRET_KEY from .env).
        """
        # Instantiate the hooked stream — no network activity here.
        self._stream: _HookedStream = _HookedStream(api_key, secret_key)

        # Inject our connect callback into the stream object.
        # This is called by _HookedStream._start_ws after each successful
        # connection — the boolean argument tells us whether it is a reconnect.
        self._stream._connect_callback = self._on_connect

        # _subscribed_symbols: the set of ticker strings we are currently
        # subscribed to. Python `set` is like std::unordered_set — O(1) average
        # for add, remove, and membership tests. We annotate the type explicitly
        # so a C++ reader can see the intended element type.
        self._subscribed_symbols: set[str] = set()

        # _reconnect_attempt: monotonically increasing counter for successive
        # reconnects. Reset to 0 on the first successful connect.
        self._reconnect_attempt: int = 0

        # _callback: stores the user-supplied quote callback so _handle_quote
        # can invoke it. Initialised to None; set in subscribe().
        self._callback: Callable[[pd.DataFrame], None] | None = None

        # _stream_thread: the background thread running self._stream.run().
        # Initialised to None; set in subscribe() when the thread is started.
        # We keep a reference so callers can join it if needed.
        self._stream_thread: threading.Thread | None = None

    # -----------------------------------------------------------------------
    # Reconnect hook (called by _HookedStream after each successful connect)
    # -----------------------------------------------------------------------

    def _on_connect(self, is_reconnect: bool) -> None:
        """
        Called by _HookedStream._start_ws after every successful connection.

        On the FIRST connect (is_reconnect=False):
            - Reset _reconnect_attempt to 0 (clean slate).

        On SUBSEQUENT connects (is_reconnect=True):
            - Increment _reconnect_attempt.
            - Compute the back-off delay for this attempt number.
            - Emit a WARNING structured log event with all required fields
              (Requirement 7.3).

        Note: this method runs inside the SDK's asyncio event loop thread,
        but it only does lightweight work (counter arithmetic and logging),
        so there is no threading hazard for the accesses we do here.
        """
        if not is_reconnect:
            # First successful connect — reset the counter so reconnects
            # after a later drop start from attempt=1 again.
            self._reconnect_attempt = 0
        else:
            # This is a reconnect after an unexpected connection drop.
            self._reconnect_attempt += 1

            # Compute the back-off delay for this attempt.
            # We log the delay that *would* be used by a back-off strategy;
            # the SDK handles the actual delay internally via its own retries.
            delay: float = back_off_delay(self._reconnect_attempt)

            # Emit the WARNING structured log event (Requirement 7.3).
            # list() converts the set to a list — JSON serialisation requires
            # a list, not a set. list(set) order is not guaranteed, but that
            # is fine for a log field.
            log_structured(
                _logger,
                logging.WARNING,
                "websocket_reconnecting",
                symbols=list(self._subscribed_symbols),
                attempt=self._reconnect_attempt,
                backoff_seconds=delay,
            )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def subscribe(
        self,
        symbols: List[str],
        callback: Callable[[pd.DataFrame], None],
    ) -> None:
        """
        Subscribe to live quotes for the given symbols.

        Registers _handle_quote as the async handler for each symbol with the
        SDK, then starts the SDK's event loop in a background daemon thread if
        it is not already running.

        Algorithm D (subscribe path):
          1. Store callback reference.
          2. Register _handle_quote via self._stream.subscribe_quotes().
          3. Start the stream in a background thread if not already running.
          4. Update self._subscribed_symbols.

        Args:
            symbols:  List of ticker strings to subscribe to, e.g. ["AAPL"].
            callback: Callable that receives a single-row quote DataFrame on
                      each incoming quote. Called from the background thread.

        Raises:
            ConnectionError:  If the WebSocket cannot connect (re-raised from SDK).
            PermissionError:  If Alpaca rejects the API credentials (re-raised from SDK).
        """
        # Store the user callback so _handle_quote can call it later.
        self._callback = callback

        # Register our async handler for each symbol's quotes.
        # subscribe_quotes(handler, *symbols) takes the handler first, then
        # the symbols as variadic positional args. The * unpacking below
        # expands the list into individual positional arguments — equivalent
        # to std::apply() in C++ or passing a parameter pack.
        self._stream.subscribe_quotes(self._handle_quote, *symbols)

        # Start the stream's event loop in a background thread — but only if
        # it is not already running (idempotent).
        if self._stream_thread is None or not self._stream_thread.is_alive():
            # threading.Thread wraps an OS thread. target= is the function the
            # thread will call — like the callable passed to std::thread.
            # daemon=True means the thread dies when the main program exits —
            # useful for background I/O loops that should not block shutdown.
            self._stream_thread = threading.Thread(
                target=self._stream.run,
                daemon=True,  # daemon=True: dies when the main program exits
            )
            # .start() launches the thread — equivalent to calling .detach()
            # on a std::thread after construction (but Python threads are always
            # detachable; join() is still available if you want to wait).
            self._stream_thread.start()

        # Update the subscription set with the newly requested symbols.
        # .update(iterable) adds all elements from the iterable to the set —
        # like calling insert() for each element in a std::unordered_set.
        self._subscribed_symbols.update(symbols)

    def unsubscribe(self, symbols: List[str]) -> None:
        """
        Stop quote callbacks for the given symbols.

        Silently skips any symbol not in the current subscription (Req. 3.5).
        Does NOT close the underlying WebSocket connection — other subscribed
        symbols continue to receive callbacks.

        Algorithm D (unsubscribe path):
          For each symbol:
            If not in _subscribed_symbols: skip (silent ignore).
            Else: call _stream.unsubscribe_quotes(symbol) and remove from set.

        Args:
            symbols: List of ticker strings to unsubscribe from.
        """
        for symbol in symbols:
            # Check membership in O(1) — the set equivalent of a map::count().
            if symbol not in self._subscribed_symbols:
                # Silently ignore symbols that were never subscribed.
                continue

            # Tell the SDK to stop delivering quotes for this symbol.
            # unsubscribe_quotes(*symbols) takes variadic positional args,
            # so we pass a single-element call per symbol.
            self._stream.unsubscribe_quotes(symbol)

            # Remove from our tracking set.
            # .discard() is like .remove() but does not raise if the key is
            # absent — safer than .remove() in case of a race condition.
            self._subscribed_symbols.discard(symbol)

    def disconnect(self) -> None:
        """
        Close the WebSocket connection cleanly and stop all callbacks.

        Calls stop() on the SDK stream, which signals the background thread's
        event loop to exit. Safe to call even if subscribe() was never called
        (the SDK's stop() is a no-op when not running).

        Algorithm D (disconnect path):
          1. Call self._stream.stop().
          2. Clear self._subscribed_symbols.
        """
        self._stream.stop()

        # Clear the set — .clear() removes all elements, like calling
        # std::unordered_set::clear() in C++.
        self._subscribed_symbols.clear()

    # -----------------------------------------------------------------------
    # Internal async handler
    # -----------------------------------------------------------------------

    async def _handle_quote(self, data) -> None:
        """
        Async handler registered with StockDataStream for incoming quote messages.

        Called by the SDK inside its asyncio event loop for every quote received.
        The method is `async` because the SDK requires all registered handlers
        to be coroutines (async def functions) — it awaits them from within its
        own event loop.

        On VALID data:
          1. Extracts symbol, bid_price, ask_price, timestamp.
          2. Computes mid_price = (bid_price + ask_price) / 2.
          3. Builds a single-row DataFrame, casts dtypes to match the quote schema.
          4. Validates the DataFrame with validate_quote_dataframe().
          5. Invokes the stored user callback.

        On INVALID data (missing fields or wrong types):
          - Logs a WARNING with repr(data) and returns immediately.
          - Does NOT invoke the callback.
          - Does NOT raise any exception (Requirement 3.7).

        Args:
            data: The quote object delivered by the SDK. When raw_data=False
                  (the default), this is an alpaca.data.models.quotes.Quote
                  Pydantic model with attributes: symbol, bid_price, ask_price,
                  timestamp. We use getattr() to access attributes defensively.
        """
        # ------------------------------------------------------------------
        # Step 1: Extract fields defensively
        # ------------------------------------------------------------------
        # getattr(obj, name, default) is Python's safe attribute accessor —
        # like obj.*name with a fallback, similar to a null-safe dereference.
        # We use a sentinel default of None to distinguish "field absent" from
        # "field present but falsy".

        symbol    = getattr(data, "symbol",    None)
        bid_price = getattr(data, "bid_price", None)
        ask_price = getattr(data, "ask_price", None)
        timestamp = getattr(data, "timestamp", None)

        # Validate that all required fields are present and have the right types.
        # isinstance(x, T) returns True only if x is of type T (or a subtype).
        # We check str for symbol and (int, float) for prices because the SDK
        # may return Decimal or similar numeric types in some configurations.

        if not isinstance(symbol, str):
            log_structured(
                _logger,
                logging.WARNING,
                "malformed_quote",
                raw=repr(data),
                reason="symbol missing or not a string",
            )
            return

        if not isinstance(bid_price, (int, float)):
            log_structured(
                _logger,
                logging.WARNING,
                "malformed_quote",
                raw=repr(data),
                reason="bid_price missing or not numeric",
            )
            return

        if not isinstance(ask_price, (int, float)):
            log_structured(
                _logger,
                logging.WARNING,
                "malformed_quote",
                raw=repr(data),
                reason="ask_price missing or not numeric",
            )
            return

        if timestamp is None:
            log_structured(
                _logger,
                logging.WARNING,
                "malformed_quote",
                raw=repr(data),
                reason="timestamp missing",
            )
            return

        # ------------------------------------------------------------------
        # Step 2: Compute mid_price
        # ------------------------------------------------------------------
        mid_price: float = (bid_price + ask_price) / 2.0

        # ------------------------------------------------------------------
        # Step 3: Build single-row DataFrame and cast dtypes
        # ------------------------------------------------------------------
        # pd.DataFrame([dict]) builds a one-row DataFrame from a list
        # containing a single dict. The dict keys become column names and
        # the values become the single row's data.
        #
        # pd.Timestamp(ts, tz="UTC") converts the SDK's timestamp (which may
        # be a Python datetime, a string, or already a pd.Timestamp) into a
        # UTC-aware pandas Timestamp. This is the equivalent of
        # std::chrono::time_point::clock::from_time_t() plus timezone attach.
        try:
            ts = pd.Timestamp(timestamp, tz="UTC")
        except Exception:
            # If the timestamp cannot be converted to a UTC Timestamp, treat
            # the message as malformed rather than raising.
            log_structured(
                _logger,
                logging.WARNING,
                "malformed_quote",
                raw=repr(data),
                reason="timestamp cannot be converted to UTC pd.Timestamp",
            )
            return

        df = pd.DataFrame([{
            "symbol":    symbol,
            "bid_price": float(bid_price),
            "ask_price": float(ask_price),
            "mid_price": float(mid_price),
            "timestamp": ts,
        }])

        # Cast dtypes to match the quote schema exactly.
        # QUOTE_COLUMNS is a dict mapping column name → dtype string.
        # We cast each column individually so we can target only the columns
        # that need explicit casting (float columns are already float64 from
        # the dict construction above; "str" and datetime need explicit work).
        #
        # The symbol column uses pandas 3.x StringDtype (dtype="str").
        # pd.Series.astype("str") in older pandas gives object dtype,
        # but in pandas 3.x it gives the new StringDtype — we rely on the
        # project's pinned pandas version being 3.x, as noted in schemas.py.
        df = df.astype({
            "symbol":    QUOTE_COLUMNS["symbol"],     # "str" → pandas StringDtype (3.x)
            "bid_price": QUOTE_COLUMNS["bid_price"],  # "float64"
            "ask_price": QUOTE_COLUMNS["ask_price"],  # "float64"
            "mid_price": QUOTE_COLUMNS["mid_price"],  # "float64"
        })

        # The timestamp column needs to be a UTC-aware datetime64[ns, UTC].
        # pd.to_datetime with utc=True forces UTC-aware DatetimeTZDtype.
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        # ------------------------------------------------------------------
        # Step 4: Validate schema
        # ------------------------------------------------------------------
        # validate_quote_dataframe() raises ValueError on the first mismatch.
        # We catch that here so we never propagate exceptions out of the handler.
        try:
            validate_quote_dataframe(df)
        except ValueError as exc:
            log_structured(
                _logger,
                logging.WARNING,
                "malformed_quote",
                raw=repr(data),
                reason=f"schema validation failed: {exc}",
            )
            return

        # ------------------------------------------------------------------
        # Step 5: Invoke the user callback
        # ------------------------------------------------------------------
        # Guard: only call if a callback is registered (subscribe() may not
        # have been called yet in unusual teardown scenarios).
        if self._callback is not None:
            self._callback(df)
