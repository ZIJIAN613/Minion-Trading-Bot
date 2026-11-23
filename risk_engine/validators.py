# risk_engine/validators.py
#
# Input validation functions for the risk engine module.
# These are pure functions — no I/O, no side effects, no network access.
#
# Each validator checks its input in a defined order (fail-fast) and raises
# TypeError or ValueError on the first problem found.  This mirrors the
# strategy/validators.py pattern but validates signal dictionaries and
# account snapshots instead of bar DataFrames.
#
# IMPORTANT PYTHON QUIRK FOR C++ DEVELOPERS:
# In Python, `bool` is a subclass of `int`.  That means `isinstance(True, int)`
# returns True!  We must explicitly check `isinstance(x, bool)` BEFORE checking
# `isinstance(x, (int, float))` to reject booleans from numeric fields.
# This is unlike C++ where bool and int are distinct types.

import pandas as pd  # pandas Timestamp — used for timestamp validation


# ---------------------------------------------------------------------------
# validate_signal_dict
# ---------------------------------------------------------------------------

def validate_signal_dict(signal_dict) -> None:
    """
    Validate the signal dictionary from the strategy module.

    Checks (in order — fail-fast, stops at first failure):
      1. signal_dict is a dict (not None or other type) → TypeError
      2. Contains 'signal' key with value in {"BUY", "SELL", "HOLD"} → ValueError
      3. Contains 'timestamp' key with UTC-aware pandas Timestamp → ValueError
      4. If 'symbol' key present, value is str of 1–10 chars → ValueError

    Args:
        signal_dict: The signal dictionary to validate.

    Raises:
        TypeError:  If signal_dict is not a Python dict.
        ValueError: If required keys are missing or values are invalid.
    """

    # ------------------------------------------------------------------
    # Step 1: Check that signal_dict is actually a Python dict
    # ------------------------------------------------------------------
    # isinstance(x, dict) is Python's equivalent of dynamic_cast<dict*>(x).
    # Returns True only if x is of type dict or a subclass of dict.
    if not isinstance(signal_dict, dict):
        # type(x).__name__ gives the human-readable class name (like typeid in C++)
        raise TypeError(
            f"signal_dict must be a dict, but got {type(signal_dict).__name__}"
        )

    # ------------------------------------------------------------------
    # Step 2: Check 'signal' key — must exist and be one of three literals
    # ------------------------------------------------------------------
    # The set {"BUY", "SELL", "HOLD"} is Python's equivalent of
    # std::unordered_set<std::string>{"BUY", "SELL", "HOLD"}.
    # The `in` operator checks membership in O(1) for sets.
    VALID_SIGNALS = {"BUY", "SELL", "HOLD"}

    if "signal" not in signal_dict:
        raise ValueError(
            "signal_dict is missing required key 'signal'"
        )

    if signal_dict["signal"] not in VALID_SIGNALS:
        raise ValueError(
            f"signal_dict key 'signal' has invalid value: "
            f"{signal_dict['signal']!r}; must be one of {sorted(VALID_SIGNALS)}"
        )

    # ------------------------------------------------------------------
    # Step 3: Check 'timestamp' key — must be a timezone-aware pd.Timestamp
    # ------------------------------------------------------------------
    # pd.Timestamp is pandas' high-resolution datetime type.  A "timezone-aware"
    # Timestamp has .tzinfo set (not None), meaning it knows what timezone it's in.
    # This is similar to C++20's std::chrono::zoned_time vs. sys_time.
    if "timestamp" not in signal_dict:
        raise ValueError(
            "signal_dict is missing required key 'timestamp'"
        )

    ts = signal_dict["timestamp"]

    # Check that it's actually a pandas Timestamp (not a datetime, not a string)
    if not isinstance(ts, pd.Timestamp):
        raise ValueError(
            f"signal_dict key 'timestamp' must be a timezone-aware pandas "
            f"Timestamp, but got {type(ts).__name__}"
        )

    # Check that the Timestamp is timezone-aware (has .tzinfo set)
    # .tzinfo is None for naive (no-timezone) timestamps.
    if ts.tzinfo is None:
        raise ValueError(
            "signal_dict key 'timestamp' must be timezone-aware, "
            "but got a timezone-naive pandas Timestamp"
        )

    # ------------------------------------------------------------------
    # Step 4: Check optional 'symbol' key — if present, must be str 1–10 chars
    # ------------------------------------------------------------------
    # `in` on a dict checks key existence in O(1), like std::map::count().
    # The symbol key is OPTIONAL — if absent, that's perfectly fine.
    if "symbol" in signal_dict:
        symbol = signal_dict["symbol"]

        # Must be a string
        if not isinstance(symbol, str):
            raise ValueError(
                f"signal_dict key 'symbol' must be a string, "
                f"but got {type(symbol).__name__}"
            )

        # Must be between 1 and 10 characters (inclusive)
        # len() returns the length of a string, like std::string::size().
        if len(symbol) < 1 or len(symbol) > 10:
            raise ValueError(
                f"signal_dict key 'symbol' must be 1–10 characters, "
                f"but got length {len(symbol)}"
            )


# ---------------------------------------------------------------------------
# validate_current_price
# ---------------------------------------------------------------------------

def validate_current_price(current_price) -> None:
    """
    Validate that current_price is a positive number.

    Checks:
      1. current_price is int or float (not bool, not None) → ValueError
      2. current_price > 0 → ValueError

    Note: This raises ValueError (not TypeError) even for wrong types,
    because the requirements specify a single ValueError message for all
    invalid current_price values.

    Args:
        current_price: The market price to validate.

    Raises:
        ValueError: If current_price is None, non-numeric, boolean, zero,
                    or negative.
    """

    # ------------------------------------------------------------------
    # Step 1: Reject booleans first (Python quirk: bool is subclass of int)
    # ------------------------------------------------------------------
    # In Python, True == 1 and False == 0, and isinstance(True, int) is True.
    # We must check for bool BEFORE checking for int/float, otherwise True
    # would pass the numeric check below.  C++ doesn't have this issue since
    # bool and int are separate types.
    if isinstance(current_price, bool):
        raise ValueError("current_price must be a positive number")

    # ------------------------------------------------------------------
    # Step 2: Check that it's actually a number (int or float)
    # ------------------------------------------------------------------
    # isinstance(x, (int, float)) checks if x is either an int or float.
    # This is like: dynamic_cast<int*>(x) || dynamic_cast<float*>(x)
    if not isinstance(current_price, (int, float)):
        raise ValueError("current_price must be a positive number")

    # ------------------------------------------------------------------
    # Step 3: Check that the number is strictly positive (> 0)
    # ------------------------------------------------------------------
    if current_price <= 0:
        raise ValueError("current_price must be a positive number")


# ---------------------------------------------------------------------------
# validate_account_snapshot
# ---------------------------------------------------------------------------

def validate_account_snapshot(account_snapshot) -> None:
    """
    Validate the account snapshot dictionary.

    Checks (in order — fail-fast, stops at first failure):
      1. account_snapshot is a dict (not None) → TypeError
      2. Contains 'equity' key: positive number (int or float, not bool) → ValueError
      3. Contains 'cash' key: non-negative number (int or float, not bool) → ValueError
      4. Contains 'open_positions' key: list of dicts → ValueError
         Each position dict must have 'symbol' (non-empty str) and
         'size' (positive int, not bool).
      5. Contains 'daily_realized_pnl' key: number (int or float, not bool) → ValueError

    Args:
        account_snapshot: The account state dictionary to validate.

    Raises:
        TypeError:  If account_snapshot is not a Python dict.
        ValueError: If required keys are missing or values are invalid.
    """

    # ------------------------------------------------------------------
    # Step 1: Check that account_snapshot is actually a Python dict
    # ------------------------------------------------------------------
    if not isinstance(account_snapshot, dict):
        raise TypeError(
            f"account_snapshot must be a dict, but got "
            f"{type(account_snapshot).__name__}"
        )

    # ------------------------------------------------------------------
    # Step 2: Check 'equity' key — must exist and be a positive number
    # ------------------------------------------------------------------
    if "equity" not in account_snapshot:
        raise ValueError(
            "account_snapshot is missing required key 'equity'"
        )

    equity = account_snapshot["equity"]

    # Reject booleans first (Python quirk: bool is subclass of int)
    if isinstance(equity, bool):
        raise ValueError(
            f"account_snapshot key 'equity' must be a positive number, "
            f"but got bool"
        )

    # Must be int or float
    if not isinstance(equity, (int, float)):
        raise ValueError(
            f"account_snapshot key 'equity' must be a positive number, "
            f"but got {type(equity).__name__}"
        )

    # Must be strictly positive (> 0)
    if equity <= 0:
        raise ValueError(
            f"account_snapshot key 'equity' must be positive, "
            f"but got {equity}"
        )

    # ------------------------------------------------------------------
    # Step 3: Check 'cash' key — must exist and be a non-negative number
    # ------------------------------------------------------------------
    if "cash" not in account_snapshot:
        raise ValueError(
            "account_snapshot is missing required key 'cash'"
        )

    cash = account_snapshot["cash"]

    # Reject booleans first
    if isinstance(cash, bool):
        raise ValueError(
            f"account_snapshot key 'cash' must be a non-negative number, "
            f"but got bool"
        )

    # Must be int or float
    if not isinstance(cash, (int, float)):
        raise ValueError(
            f"account_snapshot key 'cash' must be a non-negative number, "
            f"but got {type(cash).__name__}"
        )

    # Must be non-negative (>= 0)
    if cash < 0:
        raise ValueError(
            f"account_snapshot key 'cash' must be non-negative, "
            f"but got {cash}"
        )

    # ------------------------------------------------------------------
    # Step 4: Check 'open_positions' key — must be a list of valid dicts
    # ------------------------------------------------------------------
    if "open_positions" not in account_snapshot:
        raise ValueError(
            "account_snapshot is missing required key 'open_positions'"
        )

    positions = account_snapshot["open_positions"]

    # Must be a list (Python's equivalent of std::vector)
    if not isinstance(positions, list):
        raise ValueError(
            f"account_snapshot key 'open_positions' must be a list, "
            f"but got {type(positions).__name__}"
        )

    # Validate each position in the list.
    # enumerate() gives both the index and the element — like a counted for-loop
    # in C++:  for (size_t i = 0; i < vec.size(); ++i) { auto& pos = vec[i]; }
    for i, pos in enumerate(positions):
        # Each position must be a dict
        if not isinstance(pos, dict):
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' must be a dict, "
                f"but got {type(pos).__name__}"
            )

        # Each position dict must have 'symbol' — a non-empty string
        if "symbol" not in pos:
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' is missing "
                f"required key 'symbol'"
            )

        pos_symbol = pos["symbol"]

        if not isinstance(pos_symbol, str):
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' key 'symbol' "
                f"must be a non-empty string, but got {type(pos_symbol).__name__}"
            )

        if len(pos_symbol) == 0:
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' key 'symbol' "
                f"must be a non-empty string, but got empty string"
            )

        # Each position dict must have 'size' — a positive int (not bool)
        if "size" not in pos:
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' is missing "
                f"required key 'size'"
            )

        pos_size = pos["size"]

        # Reject booleans first (bool is subclass of int in Python)
        if isinstance(pos_size, bool):
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' key 'size' "
                f"must be a positive integer, but got bool"
            )

        # Must be an int (not float — shares are whole numbers)
        if not isinstance(pos_size, int):
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' key 'size' "
                f"must be a positive integer, but got {type(pos_size).__name__}"
            )

        # Must be strictly positive (>= 1 share)
        if pos_size <= 0:
            raise ValueError(
                f"account_snapshot 'open_positions[{i}]' key 'size' "
                f"must be positive, but got {pos_size}"
            )

    # ------------------------------------------------------------------
    # Step 5: Check 'daily_realized_pnl' key — must be a number (float or int)
    # ------------------------------------------------------------------
    if "daily_realized_pnl" not in account_snapshot:
        raise ValueError(
            "account_snapshot is missing required key 'daily_realized_pnl'"
        )

    pnl = account_snapshot["daily_realized_pnl"]

    # Reject booleans first
    if isinstance(pnl, bool):
        raise ValueError(
            f"account_snapshot key 'daily_realized_pnl' must be a number, "
            f"but got bool"
        )

    # Must be int or float (can be negative — that means a loss today)
    if not isinstance(pnl, (int, float)):
        raise ValueError(
            f"account_snapshot key 'daily_realized_pnl' must be a number, "
            f"but got {type(pnl).__name__}"
        )
