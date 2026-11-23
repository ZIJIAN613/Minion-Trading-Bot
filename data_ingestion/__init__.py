# data_ingestion/__init__.py
#
# Public API surface for the data_ingestion module.
#
# This file is the ONLY file that code outside data_ingestion/ should touch.
# It wires together credentials, config, RestClient, and StreamClient into a
# single clean interface: DataIngestionModule.
#
# ============================================================
# READ-ONLY ENFORCEMENT — CRITICAL SAFETY CONSTRAINT
# ============================================================
# This file MUST NEVER import TradingClient or BrokerClient.
# The data_ingestion module is strictly read-only — it may only
# retrieve market data, never place, modify, or cancel orders.
# (Requirements 4.1, 4.2, 4.3)
# ============================================================
#
# Requirements: 1.1, 1.2, 1.3, 4.3, 7.1, 7.4

import logging
import traceback    # Python stdlib: provides traceback.format_exc() which
                    # captures the current exception's full stack trace as a
                    # string — equivalent to printing a C++ stack trace via
                    # <execinfo.h> or a framework like Abseil's symbolise.

import pathlib      # Python's OOP path API — like std::filesystem::path in C++17.
                    # We use it to resolve config.yaml's path relative to THIS
                    # file, regardless of the current working directory.

from typing import Callable, List

import pandas as pd

# Internal module imports — all relative to this package.
# "from data_ingestion.X import Y" is the absolute form of relative imports;
# it matches what the rest of the codebase uses and is unambiguous.
from data_ingestion.credentials import load_credentials
from data_ingestion.config_loader import load_config
from data_ingestion.rest_client import RestClient
from data_ingestion.stream_client import StreamClient
from data_ingestion.logger import get_logger, log_structured


# Module-level logger — uses the root package name so all data_ingestion
# loggers form one hierarchy, controllable from a single root logger.
_logger = get_logger("data_ingestion")


class DataIngestionModule:
    """
    Public entry point for the data_ingestion module.

    This is the ONLY class that code outside data_ingestion/ should
    instantiate. It owns a RestClient and a StreamClient and delegates
    all work to them.

    Typical usage (mirrors a C++ facade pattern):
        module = DataIngestionModule()
        module.initialise()   # must be called first
        df = module.get_historical_bars("AAPL", "1d", "2024-01-01", "2024-06-01")
        module.subscribe(["AAPL", "SPY"], my_callback)
        ...
        module.disconnect()

    Error handling contract (Requirement 7.4):
        Every public method wraps its body in a try/except that:
          1. Emits an ERROR structured log event "unhandled_exception" with
             exception_type, message, and stack_trace fields.
          2. Re-raises the original exception unchanged.
        This means the caller always sees the real exception AND a log entry
        is always written — regardless of which public method raised.
    """

    def __init__(self) -> None:
        """
        Construct a DataIngestionModule in its uninitialised state.

        The REST and stream clients are NOT created here — they require
        credentials and config that are loaded during initialise().
        Separating construction from initialisation is a deliberate design
        choice (like a C++ two-phase initialisation pattern) so that object
        construction can never fail with a network or I/O error.
        """
        # _rest_client and _stream_client start as None.
        # Python's type annotation "X | None" is equivalent to
        # std::optional<X> in C++ — it signals "may or may not be set".
        # The actual objects are assigned in initialise().
        self._rest_client: RestClient | None = None
        self._stream_client: StreamClient | None = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def initialise(self) -> None:
        """
        Load credentials and config, then build the REST and stream clients.

        Must be called before any other method. Safe to call once — if called
        again it will reload credentials and config and rebuild the clients.

        Steps (mirrors Algorithm A + B + module init from the design doc):
          1. Load credentials via load_credentials().
          2. Resolve config.yaml path using pathlib.
          3. Load and validate config via load_config().
          4. Build RestClient(api_key, secret_key).
          5. Build StreamClient(api_key, secret_key).
          6. Emit INFO log "module_initialised" with symbols and timeframe.

        Raises:
            EnvironmentError:  If .env is missing or credentials are absent.
            FileNotFoundError: If config.yaml is missing.
            ValueError:        If config.yaml is malformed or has invalid values.
        """
        try:
            # ------------------------------------------------------------------
            # Step 1: Load credentials from .env
            # ------------------------------------------------------------------
            # load_credentials() calls dotenv.load_dotenv() internally, then
            # validates and returns (api_key, secret_key).
            # Python tuple unpacking: "a, b = func()" is equivalent to
            #   auto [a, b] = func();  (C++17 structured bindings)
            api_key, secret_key = load_credentials()

            # ------------------------------------------------------------------
            # Step 2: Resolve config.yaml path
            # ------------------------------------------------------------------
            # pathlib.Path(__file__) gives the absolute path to THIS file
            # (data_ingestion/__init__.py).
            # .parent gives the directory containing it (data_ingestion/).
            # We then append "config.yaml" using the / operator — pathlib
            # overloads / for path joining (like std::filesystem::path / in C++17).
            #
            # Using __file__-relative paths means config.yaml is always found
            # at data_ingestion/config.yaml regardless of what the current
            # working directory is when the program runs — robust against being
            # launched from any directory.
            config_path: str = str(
                pathlib.Path(__file__).parent / "config.yaml"
            )

            # ------------------------------------------------------------------
            # Step 3: Load and validate configuration
            # ------------------------------------------------------------------
            # load_config() reads config.yaml, validates every field, and
            # returns a typed Config dataclass (like a C++ struct with __init__
            # auto-generated by @dataclass).
            config = load_config(config_path)

            # ------------------------------------------------------------------
            # Step 4: Build RestClient
            # ------------------------------------------------------------------
            # RestClient uses StockHistoricalDataClient internally — the
            # market-data-only Alpaca client that connects to
            # https://data.alpaca.markets (NOT the trading endpoints).
            self._rest_client = RestClient(api_key, secret_key)

            # ------------------------------------------------------------------
            # Step 5: Build StreamClient
            # ------------------------------------------------------------------
            # StreamClient uses StockDataStream internally — the WebSocket
            # streaming client. The connection is NOT opened here; it is opened
            # lazily on the first subscribe() call.
            self._stream_client = StreamClient(api_key, secret_key)

            # ------------------------------------------------------------------
            # Step 6: Emit INFO log "module_initialised" (Requirement 7.1)
            # ------------------------------------------------------------------
            # log_structured() emits a single-line JSON record to stderr.
            # The fields symbols and timeframe come from the validated config.
            log_structured(
                _logger,
                logging.INFO,
                "module_initialised",
                symbols=config.symbols,      # list of ticker strings, e.g. ["AAPL", "SPY"]
                timeframe=config.timeframe,  # e.g. "1d"
            )

        except Exception as exc:
            # Requirement 7.4: catch any unhandled exception, log it with full
            # context, then re-raise so the caller sees the original error.
            _emit_unhandled_exception_log(exc)
            raise  # re-raise the SAME exception — "raise" with no argument
                   # preserves the original exception type, message, and
                   # traceback (unlike "raise exc" which would reset the
                   # traceback to this line). This is a subtle Python idiom:
                   # bare "raise" inside an except block == C++'s "throw;" (rethrow).

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candle bars for one symbol.

        Delegates entirely to self._rest_client.get_historical_bars().
        See RestClient.get_historical_bars() for full documentation.

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
        try:
            # Delegate directly to the REST client — no logic here, just
            # forwarding. This keeps the public API thin and the logic in
            # the appropriate specialised component.
            return self._rest_client.get_historical_bars(
                symbol, timeframe, start, end
            )
        except Exception as exc:
            _emit_unhandled_exception_log(exc)
            raise

    def subscribe(
        self,
        symbols: List[str],
        callback: Callable[[pd.DataFrame], None],
    ) -> None:
        """
        Begin streaming live quotes for the given symbols.

        The callback is invoked in a background thread for each incoming
        quote. See StreamClient.subscribe() for full documentation.

        Args:
            symbols:  List of ticker strings to subscribe to.
            callback: Function receiving a single-row quote DataFrame on each
                      incoming quote. Called from a background thread.

        Raises:
            ConnectionError:  If the WebSocket cannot connect.
            PermissionError:  If Alpaca rejects the API credentials.
        """
        try:
            self._stream_client.subscribe(symbols, callback)
        except Exception as exc:
            _emit_unhandled_exception_log(exc)
            raise

    def unsubscribe(self, symbols: List[str]) -> None:
        """
        Stop quote callbacks for the given symbols.

        Silently ignores symbols that were not subscribed.
        Does not close the underlying WebSocket connection.

        Args:
            symbols: List of ticker strings to unsubscribe from.
        """
        try:
            self._stream_client.unsubscribe(symbols)
        except Exception as exc:
            _emit_unhandled_exception_log(exc)
            raise

    def disconnect(self) -> None:
        """
        Close the WebSocket connection cleanly and stop all callbacks.

        Safe to call even if subscribe() was never called — the underlying
        StreamClient.disconnect() is a no-op when not running.
        """
        try:
            self._stream_client.disconnect()
        except Exception as exc:
            _emit_unhandled_exception_log(exc)
            raise


# ---------------------------------------------------------------------------
# Private helper — unhandled exception logging (Requirement 7.4)
# ---------------------------------------------------------------------------

def _emit_unhandled_exception_log(exc: Exception) -> None:
    """
    Emit a structured ERROR log for an unhandled exception.

    Called inside every public method's except block before re-raising.
    Centralising this logic in one helper avoids repeating the same
    log_structured() call in five places and keeps each method's except
    block to a single call.

    The fields match Requirement 7.4 exactly:
      - event:          "unhandled_exception"
      - level:          "ERROR"   (set by the logging level argument)
      - timestamp:      UTC ISO 8601 (added automatically by log_structured)
      - exception_type: the class name of the exception, e.g. "ValueError"
      - message:        the string representation of the exception
      - stack_trace:    the full formatted traceback as a multi-line string

    Args:
        exc: The exception that was caught. Used for exception_type and message.
             The stack_trace is read from the current exception context via
             traceback.format_exc() — this must be called from inside an
             except block to capture the active traceback.
    """
    # type(exc).__name__ is Python's way to get the class name as a string —
    # equivalent to typeid(exc).name() in C++ (minus the mangling).
    # Example: type(ValueError("bad input")).__name__ == "ValueError"
    exception_type: str = type(exc).__name__

    # str(exc) gives the human-readable exception message — same as what
    # Python prints after "ValueError:" in a traceback.
    message: str = str(exc)

    # traceback.format_exc() captures the FULL formatted traceback of the
    # currently-active exception as a single multi-line string, including
    # file names, line numbers, and the exception message.
    # It must be called from inside an except block (otherwise it returns
    # "NoneType: None" because there is no active exception).
    stack_trace: str = traceback.format_exc()

    log_structured(
        _logger,
        logging.ERROR,
        "unhandled_exception",
        exception_type=exception_type,
        message=message,
        stack_trace=stack_trace,
    )
