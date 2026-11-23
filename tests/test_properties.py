"""
tests/test_properties.py — Property-based tests for the data_ingestion module.

Five property groups, all driven by the Hypothesis library.

Hypothesis overview for C++ developers:
  - Think of @given as a parameterised test (like TEST_P in Google Test) that
    automatically generates hundreds of random inputs according to "strategies".
  - A "strategy" (st.integers, st.text, etc.) is a generator of typed random
    values — analogous to a fuzzer input generator.
  - @settings(max_examples=N) controls how many random examples are tried.
  - assume(condition) discards the current example when the condition is False,
    acting like a filter that narrows the input space without changing the
    generator itself.

Properties tested here:
  Property 1  — Bar output schema is always correct (task 7.2)
  Property 4  — Invalid inputs always raise ValueError with the argument name (task 7.5)
  Property 6  — Exponential back-off is always bounded (task 9.4)
  Property 7  — Malformed quotes never invoke callback or raise (task 9.5)
  Property 9  — Schema validation catches dtype mismatches (tasks 5.2 and 5.3)

Validates: Requirements 2.1, 2.6, 3.3, 3.7, 5.1, 5.3

Run with:  venv\\Scripts\\python.exe -m pytest tests/ -v
"""

import asyncio          # Python's built-in async I/O library (like Boost.Asio or C++20 coroutines)
import logging          # Python's standard logging module
import types            # Provides SimpleNamespace — a lightweight plain-attribute object
from datetime import date, timedelta
from unittest.mock import MagicMock  # MagicMock is like a catch-all stub/spy in C++ testing frameworks

import pandas as pd
import pytest

# @given — the core Hypothesis decorator that turns a normal test into a
# property-based test by generating random inputs automatically.
from hypothesis import assume, given, settings

# strategies (aliased as st) — the library of input generators.
# st.integers(1, 10) produces random ints in [1, 10], st.text() produces
# random strings, st.sampled_from(seq) picks uniformly from a sequence, etc.
from hypothesis import strategies as st

# Import the module items under test.
from data_ingestion.rest_client import RestClient
from data_ingestion.schemas import (
    BAR_COLUMNS,
    QUOTE_COLUMNS,
    validate_bar_dataframe,
    validate_quote_dataframe,
)
from data_ingestion.stream_client import StreamClient, back_off_delay
from data_ingestion.timeframe_map import VALID_TIMEFRAMES


# ---------------------------------------------------------------------------
# Shared helper: build a valid one-row bar DataFrame
# ---------------------------------------------------------------------------

def _make_valid_bar_df(n_rows: int = 1) -> pd.DataFrame:
    """
    Build a minimal bar DataFrame that passes validate_bar_dataframe().

    The index is a UTC-aware DatetimeIndex; columns match BAR_COLUMNS exactly.
    Used in Property 9 tests as a baseline that is then deliberately corrupted.

    Args:
        n_rows: Number of rows to generate (default 1).

    Returns:
        A valid bar DataFrame with n_rows rows.
    """
    # pd.date_range builds a sequence of evenly-spaced timestamps.
    # freq="D" means one per calendar day; tz="UTC" makes them UTC-aware.
    index = pd.date_range(start="2024-01-01", periods=n_rows, freq="D", tz="UTC")

    # Build the DataFrame column by column.
    # dict comprehension (Python) — { key: value for key in iterable } is
    # equivalent to building a std::map with a loop in C++.
    data = {
        "open":   [100.0] * n_rows,
        "high":   [105.0] * n_rows,
        "low":    [95.0]  * n_rows,
        "close":  [102.0] * n_rows,
        "volume": [1000]  * n_rows,
    }
    df = pd.DataFrame(data, index=index)

    # Cast to the exact dtypes required by the schema.
    # .astype(dict) applies per-column dtype casts in a single call.
    df = df.astype(BAR_COLUMNS)
    return df


def _make_valid_quote_df() -> pd.DataFrame:
    """
    Build a minimal single-row quote DataFrame that passes validate_quote_dataframe().

    Returns:
        A valid one-row quote DataFrame.
    """
    df = pd.DataFrame([{
        "symbol":    "AAPL",
        "bid_price": 150.0,
        "ask_price": 151.0,
        "mid_price": 150.5,
        "timestamp": pd.Timestamp("2024-01-01T10:00:00", tz="UTC"),
    }])

    # Cast symbol to StringDtype (pandas 3.x "str"), float columns to float64.
    df = df.astype({
        "symbol":    QUOTE_COLUMNS["symbol"],     # "str" → pandas StringDtype
        "bid_price": "float64",
        "ask_price": "float64",
        "mid_price": "float64",
    })
    # Cast the timestamp column to UTC-aware datetime.
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


# ---------------------------------------------------------------------------
# Property 1 — Bar output schema is always correct
# ---------------------------------------------------------------------------
# Validates: Requirements 2.1, 5.1, 5.3
#
# For any valid (symbol, timeframe, start, end) combination, with a mocked
# Alpaca client returning a non-empty response, the returned DataFrame must
# have a UTC-aware DatetimeIndex and the correct column dtypes.

class TestProperty1BarOutputSchema:
    """
    Property 1: RestClient.get_historical_bars always returns a correctly-
    typed DataFrame regardless of which valid (symbol, timeframe, start, end)
    combination is supplied.

    Validates: Requirements 2.1, 5.1, 5.3
    """

    @staticmethod
    def _build_rest_client_with_mock(fake_df: pd.DataFrame) -> RestClient:
        """
        Create a RestClient whose internal Alpaca client is replaced with a mock.

        We bypass RestClient.__init__ (which would call the real
        StockHistoricalDataClient constructor and require live API keys) using
        object.__new__.

        object.__new__(cls) allocates a new instance of cls WITHOUT calling
        __init__ — in C++ terms it is the equivalent of allocating raw memory
        for the object (operator new) without invoking the constructor.  We
        then manually set all attributes that __init__ would have set.

        The mock's .get_stock_bars() method returns a fake response whose .df
        property yields the pre-built DataFrame, which the code under test then
        normalises and validates.
        """
        # Allocate a RestClient instance without running __init__.
        client = object.__new__(RestClient)

        # Build a mock that pretends to be a StockHistoricalDataClient.
        # MagicMock() creates an object that accepts any attribute access or
        # call and records them — similar to a Google Mock EXPECT_CALL() stub.
        mock_alpaca = MagicMock()

        # Configure mock_alpaca.get_stock_bars(...).df to return our fake_df.
        # MagicMock supports attribute chaining: mock.method(...).df is itself
        # a MagicMock by default; we set its return_value so .df becomes our df.
        mock_alpaca.get_stock_bars.return_value.df = fake_df

        # Inject the mock as the internal client (_client is what RestClient
        # uses internally for all Alpaca SDK calls).
        client._client = mock_alpaca

        return client

    # @given — generates (symbol, timeframe, start_date, day_offset) tuples.
    # All four strategies run independently; Hypothesis combines them.
    @given(
        # st.text with whitelist_categories=("Lu",) restricts to Unicode
        # uppercase letters (like [A-Z] in ASCII + international uppercase).
        # min_size=1 guarantees at least one character (non-empty symbol).
        symbol=st.text(
            alphabet=st.characters(whitelist_categories=("Lu",)),
            min_size=1,
            max_size=5,
        ),
        # st.sampled_from picks uniformly from a sequence — like choosing a
        # random element from a std::vector with a uniform distribution.
        timeframe=st.sampled_from(sorted(VALID_TIMEFRAMES)),
        # st.dates generates Python datetime.date objects in the given range.
        start_date=st.dates(
            min_value=date(2020, 1, 1),
            max_value=date(2023, 12, 31),
        ),
        # We derive end_date as start + offset days to guarantee start < end.
        day_offset=st.integers(min_value=1, max_value=30),
    )
    # @settings(max_examples=50) — Hypothesis generates 50 random combinations
    # per test run.  The default is 100; we use 50 to keep the suite fast.
    @settings(max_examples=50)
    def test_bar_output_schema_is_correct(
        self, symbol: str, timeframe: str, start_date: date, day_offset: int
    ) -> None:
        """
        **Property 1** — For any valid inputs, the returned DataFrame always has:
          - A UTC-aware DatetimeIndex
          - open, high, low, close columns of dtype float64
          - volume column of dtype int64

        Validates: Requirements 2.1, 5.1, 5.3
        """
        end_date = start_date + timedelta(days=day_offset)

        # Build a fake response DataFrame that mimics the REAL alpaca-py SDK
        # response shape: a MultiIndex of (symbol, timestamp). This is what
        # .df actually returns, even for single-symbol requests.
        fake_timestamp = pd.Timestamp("2023-01-01T00:00:00", tz="UTC")

        # pd.MultiIndex.from_tuples builds a MultiIndex from a list of tuples.
        # The names= argument labels each level — the code under test uses
        # get_level_values("timestamp") to extract the timestamp level.
        fake_multi_index = pd.MultiIndex.from_tuples(
            [(symbol, fake_timestamp)],
            names=["symbol", "timestamp"],
        )
        fake_df = pd.DataFrame(
            {
                "open":   [100.0],
                "high":   [105.0],
                "low":    [95.0],
                "close":  [102.0],
                "volume": [1000],
            },
            index=fake_multi_index,
        ).astype(BAR_COLUMNS)

        client = self._build_rest_client_with_mock(fake_df)

        # Call the real method under test with the generated inputs.
        result = client.get_historical_bars(
            symbol,
            timeframe,
            str(start_date),
            str(end_date),
        )

        # --- Assertions ---

        # The index must be a pandas DatetimeIndex (not a plain RangeIndex etc.)
        assert isinstance(result.index, pd.DatetimeIndex), (
            f"Expected DatetimeIndex, got {type(result.index).__name__}"
        )

        # The index must be timezone-aware and set to UTC.
        # .tz is None for a naive (timezone-free) index.
        assert result.index.tz is not None, "DatetimeIndex must be timezone-aware"
        assert str(result.index.tz) == "UTC", (
            f"Expected UTC timezone, got {result.index.tz}"
        )

        # Price columns must be float64.
        for col in ("open", "high", "low", "close"):
            assert str(result[col].dtype) == "float64", (
                f"Column '{col}' must be float64, got {result[col].dtype}"
            )

        # Volume must be int64.
        assert str(result["volume"].dtype) == "int64", (
            f"Column 'volume' must be int64, got {result['volume'].dtype}"
        )


# ---------------------------------------------------------------------------
# Property 4 — Invalid inputs always raise ValueError with the argument name
# ---------------------------------------------------------------------------
# Validates: Requirements 2.6
#
# Three independent sub-cases:
#   a) Empty / whitespace-only symbol
#   b) Invalid (unrecognised) timeframe string
#   c) start >= end (reversed or equal date range)

class TestProperty4InvalidInputsRaiseValueError:
    """
    Property 4: get_historical_bars raises ValueError for any invalid input,
    and the exception message always names the offending argument.

    Validates: Requirements 2.6
    """

    @staticmethod
    def _build_rest_client_no_network() -> RestClient:
        """
        Create a RestClient with a mock internal client.

        For invalid-input tests the mock will never be called because
        validation (step 1 of Algorithm C) raises before any SDK call.
        We still set it up so the object is in a valid state.
        """
        client = object.__new__(RestClient)
        client._client = MagicMock()  # Will never be called for invalid inputs
        return client

    # --- Sub-case a: empty or whitespace-only symbol ---

    @given(
        # st.sampled_from — pick from a hand-picked list of clearly bad symbols.
        # This is simpler and more readable than a complex text strategy.
        bad_symbol=st.sampled_from(["", " ", "   ", "\t", "\n", "  \t  "]),
    )
    @settings(max_examples=50)
    def test_empty_symbol_raises_value_error(self, bad_symbol: str) -> None:
        """
        **Property 4a** — An empty or whitespace-only symbol must raise
        ValueError with "symbol" in the message.

        Validates: Requirements 2.6
        """
        client = self._build_rest_client_no_network()

        # pytest.raises is a context manager that asserts the named exception
        # is raised inside the `with` block — equivalent to EXPECT_THROW in
        # Google Test.  `as exc_info` captures the exception object so we can
        # inspect its message.
        with pytest.raises(ValueError) as exc_info:
            client.get_historical_bars(bad_symbol, "1d", "2024-01-01", "2024-02-01")

        # The error message must name the offending argument.
        assert "symbol" in str(exc_info.value).lower(), (
            f"Expected 'symbol' in error message, got: {exc_info.value}"
        )

    # --- Sub-case b: unrecognised timeframe string ---

    @given(
        # st.text() generates arbitrary Unicode strings.
        # .filter(lambda s: s not in VALID_TIMEFRAMES) discards any string
        # that happens to be a valid timeframe — like a predicate filter in C++.
        bad_timeframe=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in VALID_TIMEFRAMES
        ),
    )
    @settings(max_examples=50)
    def test_invalid_timeframe_raises_value_error(self, bad_timeframe: str) -> None:
        """
        **Property 4b** — Any timeframe string not in VALID_TIMEFRAMES must
        raise ValueError with "timeframe" in the message.

        Validates: Requirements 2.6
        """
        client = self._build_rest_client_no_network()

        with pytest.raises(ValueError) as exc_info:
            client.get_historical_bars("AAPL", bad_timeframe, "2024-01-01", "2024-02-01")

        assert "timeframe" in str(exc_info.value).lower(), (
            f"Expected 'timeframe' in error message, got: {exc_info.value}"
        )

    # --- Sub-case c: start >= end ---

    @given(
        base_date=st.dates(
            min_value=date(2020, 1, 1),
            max_value=date(2023, 12, 31),
        ),
        # Offset 0 means start == end; positive offset means end is BEFORE start.
        # Both violate the "start strictly before end" requirement.
        offset=st.integers(min_value=0, max_value=30),
    )
    @settings(max_examples=50)
    def test_start_gte_end_raises_value_error(
        self, base_date: date, offset: int
    ) -> None:
        """
        **Property 4c** — When start >= end, get_historical_bars must raise
        ValueError with "start" or "end" in the message.

        We set start = base_date and end = base_date - offset, which gives
        end <= start for all offset values >= 0.

        Validates: Requirements 2.6
        """
        start_str = str(base_date)
        # Subtract offset so end <= start (equal when offset=0, before when >0).
        end_date = base_date - timedelta(days=offset)
        end_str = str(end_date)

        client = self._build_rest_client_no_network()

        with pytest.raises(ValueError) as exc_info:
            client.get_historical_bars("AAPL", "1d", start_str, end_str)

        msg = str(exc_info.value).lower()
        assert "start" in msg or "end" in msg, (
            f"Expected 'start' or 'end' in error message, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Property 6 — Exponential back-off is always bounded
# ---------------------------------------------------------------------------
# Validates: Requirements 3.3
#
# For any attempt number N >= 1:
#   delay >= 1.0
#   delay <= 60.0
#   delay == min(2^(N-1), 60.0)

class TestProperty6BackOffBounded:
    """
    Property 6: back_off_delay(N) always returns a value in [1.0, 60.0]
    that equals min(2^(N-1), 60.0) exactly.

    Validates: Requirements 3.3
    """

    # st.integers(min_value=1, max_value=100) generates attempt numbers from
    # 1 to 100, covering both the early doubling phase and the saturation cap.
    @given(attempt=st.integers(min_value=1, max_value=100))
    @settings(max_examples=50)
    def test_back_off_lower_bound(self, attempt: int) -> None:
        """
        **Property 6a** — delay must always be >= 1.0 second.

        Validates: Requirements 3.3
        """
        delay = back_off_delay(attempt)
        assert delay >= 1.0, (
            f"back_off_delay({attempt}) = {delay}, expected >= 1.0"
        )

    @given(attempt=st.integers(min_value=1, max_value=100))
    @settings(max_examples=50)
    def test_back_off_upper_bound(self, attempt: int) -> None:
        """
        **Property 6b** — delay must always be <= 60.0 seconds (cap enforced).

        Validates: Requirements 3.3
        """
        delay = back_off_delay(attempt)
        assert delay <= 60.0, (
            f"back_off_delay({attempt}) = {delay}, expected <= 60.0"
        )

    @given(attempt=st.integers(min_value=1, max_value=100))
    @settings(max_examples=50)
    def test_back_off_formula(self, attempt: int) -> None:
        """
        **Property 6c** — delay == min(2^(N-1), 60.0) exactly.

        Validates: Requirements 3.3
        """
        expected = min(2 ** (attempt - 1), 60.0)
        actual = back_off_delay(attempt)
        assert actual == expected, (
            f"back_off_delay({attempt}): expected {expected}, got {actual}"
        )


# ---------------------------------------------------------------------------
# Property 7 — Malformed quotes never invoke callback or raise
# ---------------------------------------------------------------------------
# Validates: Requirements 3.7
#
# For any message object missing required fields or having wrong-typed fields,
# _handle_quote must:
#   - NOT call the user callback
#   - NOT raise any exception
#   - Log exactly one WARNING

class TestProperty7MalformedQuotes:
    """
    Property 7: _handle_quote silently swallows malformed quote objects —
    it never calls the user callback and never raises.

    Validates: Requirements 3.7
    """

    @staticmethod
    def _build_stream_client() -> StreamClient:
        """
        Build a StreamClient instance without opening any network connections.

        Uses the same object.__new__ bypass as in Property 1 — we allocate the
        instance without calling __init__ and then manually set all instance
        attributes that _handle_quote reads.
        """
        instance = object.__new__(StreamClient)

        # MagicMock() as the callback: lets us assert it was never called.
        instance._callback = MagicMock()

        # Minimal required state for _handle_quote (it doesn't use these but
        # the handler is a method on StreamClient, so the instance must exist).
        instance._subscribed_symbols = set()
        instance._reconnect_attempt = 0
        instance._stream = MagicMock()

        return instance

    # Strategy: generate a dict with all four required fields, but give each
    # one an intentionally wrong type so _handle_quote always rejects it.
    #
    # st.fixed_dictionaries — like st.builds but for dicts; each key maps to
    # its own strategy.  st.one_of picks from two or more alternatives.
    _malformed_all_wrong_types = st.fixed_dictionaries({
        # symbol must be str, so we supply None, int, or float.
        "symbol": st.one_of(st.none(), st.integers(), st.floats(allow_nan=False)),
        # bid_price must be int or float — supply None or a string.
        "bid_price": st.one_of(st.none(), st.text(min_size=1, max_size=5)),
        # ask_price same.
        "ask_price": st.one_of(st.none(), st.text(min_size=1, max_size=5)),
        # timestamp: None is explicitly rejected by the handler.
        "timestamp": st.just(None),
    })

    @given(fields=_malformed_all_wrong_types)
    @settings(max_examples=50)
    def test_malformed_wrong_types_no_callback(self, fields: dict) -> None:
        """
        **Property 7a** — Messages with wrong-typed fields must never invoke
        the callback.

        Validates: Requirements 3.7
        """
        # types.SimpleNamespace(**d) creates a plain object whose attributes
        # are the dict keys — equivalent to a C++ struct built dynamically at
        # runtime.  The handler accesses fields via getattr(), so SimpleNamespace
        # is a perfect stand-in for the SDK's Quote Pydantic model.
        data = types.SimpleNamespace(**fields)
        instance = self._build_stream_client()

        # asyncio.run() drives an async coroutine to completion in a fresh
        # event loop — the synchronous bridge to Python's async/await world.
        # No pytest-asyncio plugin needed; the test function itself is sync.
        asyncio.run(instance._handle_quote(data))

        # The callback MagicMock tracks how many times it was called.
        # .call_count == 0 means it was never invoked.
        assert instance._callback.call_count == 0, (
            f"Callback was called {instance._callback.call_count} time(s) "
            f"for malformed data {fields!r}"
        )

    @given(fields=_malformed_all_wrong_types)
    @settings(max_examples=50)
    def test_malformed_wrong_types_no_exception(self, fields: dict) -> None:
        """
        **Property 7b** — Messages with wrong-typed fields must never raise
        any exception.

        Validates: Requirements 3.7
        """
        data = types.SimpleNamespace(**fields)
        instance = self._build_stream_client()

        # If _handle_quote raises, asyncio.run() propagates the exception and
        # pytest catches it as a test failure.  No try/except needed here.
        asyncio.run(instance._handle_quote(data))   # must not raise

    @given(fields=_malformed_all_wrong_types)
    @settings(max_examples=50)
    def test_malformed_wrong_types_logs_warning(self, fields: dict) -> None:
        """
        **Property 7c** — Each malformed message must produce exactly one
        WARNING log event.

        We cannot use pytest's caplog fixture with @given because caplog is
        function-scoped and Hypothesis does not reset it between generated
        examples.  Instead we attach a fresh logging.Handler to the module
        logger for each generated example and detach it afterwards — the same
        isolation pattern as C++ RAII: acquire on entry, release on exit.

        Validates: Requirements 3.7
        """
        # Collect log records emitted during this single invocation.
        captured: list[logging.LogRecord] = []

        # logging.Handler is the base class for all log sinks (file, stream, etc.).
        # We subclass it in-line using a lambda-based trick: define a minimal
        # subclass whose emit() method appends to our list.
        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
                captured.append(record)

        handler = _ListHandler(level=logging.WARNING)

        # Attach the handler to the stream_client logger for the duration of this
        # example.  The logger's name matches the one used inside _handle_quote.
        target_logger = logging.getLogger("data_ingestion.stream_client")
        target_logger.addHandler(handler)

        try:
            data = types.SimpleNamespace(**fields)
            instance = self._build_stream_client()
            asyncio.run(instance._handle_quote(data))
        finally:
            # Always remove our handler — even if _handle_quote raises — so we
            # don't accumulate handlers across Hypothesis examples.
            # This is the RAII cleanup step.
            target_logger.removeHandler(handler)

        warning_records = [r for r in captured if r.levelno == logging.WARNING]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 WARNING for malformed data {fields!r}, "
            f"got {len(warning_records)}: {[r.getMessage() for r in warning_records]}"
        )

    # Strategy for partially-present data: only a subset of the four required
    # attributes is present, each with an intentionally wrong type.
    _missing_fields_strategy = st.one_of(
        # Missing bid_price and ask_price — symbol is wrong type so rejected
        # at the first check.
        st.fixed_dictionaries({
            "symbol": st.one_of(st.none(), st.integers()),
        }),
        # Missing timestamp only — symbol wrong so still malformed.
        st.fixed_dictionaries({
            "symbol": st.one_of(st.none(), st.integers()),
            "bid_price": st.one_of(st.none(), st.text(min_size=1)),
        }),
        # All four keys present but symbol is non-str (rejected first).
        st.fixed_dictionaries({
            "symbol":    st.one_of(st.none(), st.integers()),
            "bid_price": st.floats(min_value=0.1, max_value=999.0, allow_nan=False),
            "ask_price": st.floats(min_value=0.1, max_value=999.0, allow_nan=False),
            "timestamp": st.just(None),
        }),
    )

    @given(fields=_missing_fields_strategy)
    @settings(max_examples=50)
    def test_missing_fields_no_callback(self, fields: dict) -> None:
        """
        **Property 7d** — Objects with missing or wrong-typed fields must
        never invoke the callback.

        Validates: Requirements 3.7
        """
        data = types.SimpleNamespace(**fields)
        instance = self._build_stream_client()

        asyncio.run(instance._handle_quote(data))

        assert instance._callback.call_count == 0, (
            f"Callback was called for partial/malformed data {fields!r}"
        )


# ---------------------------------------------------------------------------
# Property 9 — Schema validation catches dtype mismatches
# ---------------------------------------------------------------------------
# Validates: Requirements 5.3
#
# Covers both validate_bar_dataframe (task 5.2) and validate_quote_dataframe
# (task 5.3).  For each possible "one flaw" scenario — wrong dtype on a single
# column, missing column, or non-UTC index — the validator must raise ValueError
# with the column name (or index info) in the message.

class TestProperty9SchemaValidation:
    """
    Property 9: validate_bar_dataframe and validate_quote_dataframe raise
    ValueError for any single deliberate schema flaw, naming the problem in
    the message.

    Validates: Requirements 5.3
    """

    # -----------------------------------------------------------------------
    # Bar DataFrame — dtype corruption
    # -----------------------------------------------------------------------

    # st.sampled_from(list(...)) picks one column name per test run.
    @given(col=st.sampled_from(list(BAR_COLUMNS.keys())))
    @settings(max_examples=50)
    def test_bar_wrong_dtype_raises_value_error(self, col: str) -> None:
        """
        **Property 9a** — Corrupting any single bar column's dtype must raise
        ValueError whose message names that column.

        Validates: Requirements 5.3
        """
        df = _make_valid_bar_df(n_rows=2)

        # Corrupt the dtype of exactly one column.
        # We flip float64 → int32 and int64 → float64, which are both type
        # mismatches that validate_bar_dataframe should catch.
        expected_dtype = BAR_COLUMNS[col]
        if expected_dtype == "float64":
            df[col] = df[col].astype("int32")   # wrong type for price columns
        else:
            # volume is int64 — cast to float64 as the corruption
            df[col] = df[col].astype("float64")

        with pytest.raises(ValueError) as exc_info:
            validate_bar_dataframe(df)

        assert col in str(exc_info.value), (
            f"Expected column name '{col}' in error message, got: {exc_info.value}"
        )

    # -----------------------------------------------------------------------
    # Bar DataFrame — missing column
    # -----------------------------------------------------------------------

    @given(col=st.sampled_from(list(BAR_COLUMNS.keys())))
    @settings(max_examples=50)
    def test_bar_missing_column_raises_value_error(self, col: str) -> None:
        """
        **Property 9b** — Dropping any single required bar column must raise
        ValueError whose message names the missing column.

        Validates: Requirements 5.3
        """
        df = _make_valid_bar_df(n_rows=2)

        # Drop one column — like erasing a field from a C++ struct at runtime.
        # pandas DataFrame.drop(columns=[name]) returns a new DataFrame without
        # that column; we reassign to df to shadow the original.
        df = df.drop(columns=[col])

        with pytest.raises(ValueError) as exc_info:
            validate_bar_dataframe(df)

        assert col in str(exc_info.value), (
            f"Expected column name '{col}' in error message, got: {exc_info.value}"
        )

    # -----------------------------------------------------------------------
    # Bar DataFrame — non-UTC index
    # -----------------------------------------------------------------------

    def test_bar_naive_index_raises_value_error(self) -> None:
        """
        **Property 9c** — A bar DataFrame with a timezone-naive DatetimeIndex
        must raise ValueError.

        Validates: Requirements 5.3
        """
        df = _make_valid_bar_df(n_rows=2)

        # .tz_localize(None) strips the UTC timezone, making the index naive
        # (no timezone attached at all).  This is equivalent to taking a
        # std::chrono::time_point and erasing its clock association.
        df.index = df.index.tz_localize(None)

        with pytest.raises(ValueError):
            validate_bar_dataframe(df)

    def test_bar_wrong_timezone_raises_value_error(self) -> None:
        """
        **Property 9d** — A bar DataFrame with a non-UTC timezone (e.g. US/Eastern)
        must raise ValueError.

        Validates: Requirements 5.3
        """
        df = _make_valid_bar_df(n_rows=2)

        # Convert from UTC to US/Eastern — still tz-aware, but wrong timezone.
        df.index = df.index.tz_convert("US/Eastern")

        with pytest.raises(ValueError):
            validate_bar_dataframe(df)

    def test_bar_non_datetime_index_raises_value_error(self) -> None:
        """
        **Property 9e** — A bar DataFrame with a plain RangeIndex (not a
        DatetimeIndex) must raise ValueError.

        Validates: Requirements 5.3
        """
        df = _make_valid_bar_df(n_rows=2)

        # Replace the DatetimeIndex with a plain integer RangeIndex.
        # pd.RangeIndex(len(df)) produces [0, 1, 2, ...] — the default index.
        df.index = pd.RangeIndex(len(df))

        with pytest.raises(ValueError):
            validate_bar_dataframe(df)

    # -----------------------------------------------------------------------
    # Quote DataFrame — dtype corruption
    # -----------------------------------------------------------------------

    @given(col=st.sampled_from(list(QUOTE_COLUMNS.keys())))
    @settings(max_examples=50)
    def test_quote_wrong_dtype_raises_value_error(self, col: str) -> None:
        """
        **Property 9f** — Corrupting any single quote column's dtype must
        raise ValueError whose message names that column.

        Validates: Requirements 5.3
        """
        df = _make_valid_quote_df()

        expected_dtype = QUOTE_COLUMNS[col]

        # Corrupt by casting to a clearly wrong type for each expected dtype.
        if expected_dtype == "float64":
            df[col] = df[col].astype("int32")
        elif expected_dtype == "str":
            # "str" is pandas 3.x StringDtype — cast to plain object (numpy str)
            # which validate_quote_dataframe will reject as a dtype mismatch.
            df[col] = df[col].astype("object")
        elif expected_dtype == "datetime64[ns, UTC]":
            # Cast to a plain non-timezone-aware int column.
            df[col] = pd.array([0], dtype="int64")

        with pytest.raises(ValueError) as exc_info:
            validate_quote_dataframe(df)

        assert col in str(exc_info.value), (
            f"Expected column name '{col}' in error message, got: {exc_info.value}"
        )

    # -----------------------------------------------------------------------
    # Quote DataFrame — missing column
    # -----------------------------------------------------------------------

    @given(col=st.sampled_from(list(QUOTE_COLUMNS.keys())))
    @settings(max_examples=50)
    def test_quote_missing_column_raises_value_error(self, col: str) -> None:
        """
        **Property 9g** — Dropping any single required quote column must raise
        ValueError whose message names the missing column.

        Validates: Requirements 5.3
        """
        df = _make_valid_quote_df()
        df = df.drop(columns=[col])

        with pytest.raises(ValueError) as exc_info:
            validate_quote_dataframe(df)

        assert col in str(exc_info.value), (
            f"Expected column name '{col}' in error message, got: {exc_info.value}"
        )
