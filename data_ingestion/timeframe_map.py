# data_ingestion/timeframe_map.py
#
# Maps human-readable timeframe config strings (e.g. "1d") to the
# alpaca-py TimeFrame objects that the SDK's StockBarsRequest requires.

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


# dict literal — Python's equivalent of a `std::unordered_map`.
# Keys are the config strings accepted in config.yaml;
# values are the corresponding SDK TimeFrame objects.
#
# TimeFrame(amount, unit) is the alpaca-py constructor where:
#   amount — an integer (how many of the unit per bar)
#   unit   — a TimeFrameUnit enum value (Minute, Hour, or Day)
TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1m":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5m":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15m": TimeFrame(15, TimeFrameUnit.Minute),
    "30m": TimeFrame(30, TimeFrameUnit.Minute),
    "1h":  TimeFrame(1,  TimeFrameUnit.Hour),
    "4h":  TimeFrame(4,  TimeFrameUnit.Hour),
    "1d":  TimeFrame(1,  TimeFrameUnit.Day),
}

# frozenset is an immutable set — like a `const` set in C++.
# It is built once at import time and can never be modified, which
# makes it safe to share across threads and use as a dict key.
# frozenset(iterable) accepts any iterable; here we pass the dict's
# keys view, which yields every key exactly once.
VALID_TIMEFRAMES: frozenset[str] = frozenset(TIMEFRAME_MAP.keys())


def to_alpaca_timeframe(timeframe_str: str) -> TimeFrame:
    """
    Convert a config-file timeframe string to an alpaca-py TimeFrame.

    Args:
        timeframe_str: One of "1m", "5m", "15m", "30m", "1h", "4h", "1d".

    Returns:
        The corresponding alpaca-py TimeFrame object.

    Raises:
        ValueError: If timeframe_str is not one of the accepted values.
                    The error message lists every accepted value so the
                    caller knows exactly what to fix — mirrors a C++
                    assertion with a descriptive message.
    """
    # dict.get(key) returns None if the key is absent (no exception).
    # We do the explicit check ourselves so we can raise a helpful ValueError.
    if timeframe_str not in TIMEFRAME_MAP:
        accepted = ", ".join(sorted(VALID_TIMEFRAMES))
        raise ValueError(
            f"Invalid timeframe '{timeframe_str}'. "
            f"Accepted values are: {accepted}"
        )

    # Direct dict lookup — O(1) average, just like std::unordered_map::at().
    return TIMEFRAME_MAP[timeframe_str]
