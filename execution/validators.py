# execution/validators.py
#
# Input validation for the Decision_Dict received from the risk engine.
# This is a pure function — no I/O, no side effects, no network access.
#
# The validator checks its input in a strict sequential order (fail-fast)
# and raises TypeError or ValueError on the FIRST problem found.  This
# mirrors the risk_engine/validators.py pattern but validates the
# Decision_Dict structure instead of signal dictionaries.
#
# IMPORTANT PYTHON QUIRK FOR C++ DEVELOPERS:
# In Python, `bool` is a SUBCLASS of `int`.  That means:
#   isinstance(True, int)   → True   (!)
#   isinstance(False, int)  → True   (!)
# We must explicitly check `isinstance(x, bool)` BEFORE checking
# `isinstance(x, int)` or `isinstance(x, (int, float))` to reject
# booleans from numeric fields.  This is different from C++ where
# bool and int are distinct types with no inheritance relationship.

import pandas as pd  # pandas Timestamp — used for timestamp validation


# ---------------------------------------------------------------------------
# Valid value sets — defined at module level for clarity and reuse.
# These are Python sets (like C++ std::unordered_set<std::string>).
# Membership testing with `in` is O(1) average.
# ---------------------------------------------------------------------------
VALID_DECISIONS = {"APPROVED", "VETOED", "NO_ACTION"}
VALID_SIGNALS = {"BUY", "SELL", "HOLD"}


# ---------------------------------------------------------------------------
# validate_decision_dict
# ---------------------------------------------------------------------------

def validate_decision_dict(decision_dict) -> None:
    """
    Validate the Decision_Dict from the risk engine module.

    Checks are performed in strict sequential order (fail-fast):
      Step 1 — decision_dict is a dict (not None or other type)    → TypeError
      Step 2 — 'decision' key: value in {"APPROVED","VETOED","NO_ACTION"} → ValueError
      Step 3 — 'signal' key: value in {"BUY","SELL","HOLD"}        → ValueError
      Step 4 — 'timestamp' key: UTC-aware pandas Timestamp         → ValueError
      Step 5 — 'reason' key: non-empty string 1–500 chars          → ValueError
      Step 6 — (APPROVED only) 'position_size': positive int ≥ 1   → ValueError
      Step 7 — (APPROVED only) 'stop_loss': positive float > 0.0   → ValueError
      Step 8 — (APPROVED only) 'symbol': non-empty string 1–10 chars → ValueError

    Steps 6–8 are ONLY checked when decision == "APPROVED".
    Each step only executes if all preceding steps have passed.

    Args:
        decision_dict: The Decision_Dict to validate.

    Returns:
        None — returns normally if validation passes.

    Raises:
        TypeError:  If decision_dict is not a Python dict (Step 1).
        ValueError: If required keys are missing or values are invalid (Steps 2–8).
    """

    # ------------------------------------------------------------------
    # Step 1: Check that decision_dict is actually a Python dict
    # ------------------------------------------------------------------
    # isinstance(x, dict) returns True only if x is a dict or subclass.
    # This is Python's equivalent of a dynamic_cast check in C++.
    # We raise TypeError here (not ValueError) because the problem is
    # the TYPE of the input, not the VALUE of a field within it.
    if not isinstance(decision_dict, dict):
        # type(x).__name__ gives the human-readable class name
        # (like typeid(x).name() in C++, but more readable).
        raise TypeError(
            f"decision_dict must be a dict, but got "
            f"{type(decision_dict).__name__}"
        )

    # ------------------------------------------------------------------
    # Step 2: Check 'decision' key — must exist and be one of three literals
    # ------------------------------------------------------------------
    # We check for key presence first, then validate the value.
    # The `in` operator on a dict checks key existence in O(1).
    if "decision" not in decision_dict:
        raise ValueError(
            "decision_dict is missing required key 'decision'"
        )

    # Check that the value is one of the three allowed decision strings.
    # sorted(VALID_DECISIONS) in the error message provides a deterministic
    # display order (alphabetical) regardless of set insertion order.
    if decision_dict["decision"] not in VALID_DECISIONS:
        raise ValueError(
            f"decision_dict key 'decision' has invalid value: "
            f"{decision_dict['decision']!r}; "
            f"must be one of {sorted(VALID_DECISIONS)}"
        )

    # ------------------------------------------------------------------
    # Step 3: Check 'signal' key — must exist and be one of three literals
    # ------------------------------------------------------------------
    # This step only executes because Step 2 passed (fail-fast pattern).
    if "signal" not in decision_dict:
        raise ValueError(
            "decision_dict is missing required key 'signal'"
        )

    if decision_dict["signal"] not in VALID_SIGNALS:
        raise ValueError(
            f"decision_dict key 'signal' has invalid value: "
            f"{decision_dict['signal']!r}; "
            f"must be one of {sorted(VALID_SIGNALS)}"
        )

    # ------------------------------------------------------------------
    # Step 4: Check 'timestamp' key — must be a timezone-aware pd.Timestamp
    # ------------------------------------------------------------------
    # pd.Timestamp is pandas' high-resolution datetime type.
    # "Timezone-aware" means it has .tzinfo set (not None) — it knows
    # which timezone it represents.  This is analogous to C++20's
    # std::chrono::zoned_time vs plain sys_time (which has no zone info).
    #
    # We check timestamp BEFORE reason (as required by Requirement 1.4:
    # "checking timestamp before reason").
    if "timestamp" not in decision_dict:
        raise ValueError(
            "decision_dict is missing required key 'timestamp'"
        )

    ts = decision_dict["timestamp"]

    # Must be a pandas Timestamp specifically (not datetime, not string)
    if not isinstance(ts, pd.Timestamp):
        raise ValueError(
            f"decision_dict key 'timestamp' must be a timezone-aware pandas "
            f"Timestamp, but got {type(ts).__name__}"
        )

    # Must be timezone-aware (tzinfo is not None).
    # A "naive" Timestamp has .tzinfo == None — it doesn't know its timezone.
    if ts.tzinfo is None:
        raise ValueError(
            "decision_dict key 'timestamp' must be timezone-aware, "
            "but got a timezone-naive pandas Timestamp"
        )

    # ------------------------------------------------------------------
    # Step 5: Check 'reason' key — must be a non-empty string, 1–500 chars
    # ------------------------------------------------------------------
    # This step only executes because Steps 1–4 all passed.
    if "reason" not in decision_dict:
        raise ValueError(
            "decision_dict is missing required key 'reason'"
        )

    reason = decision_dict["reason"]

    # Must be a string (not None, not int, not any other type)
    if not isinstance(reason, str):
        raise ValueError(
            f"decision_dict key 'reason' must be a non-empty string "
            f"(1–500 chars), but got {type(reason).__name__}"
        )

    # len() returns the number of characters — like std::string::size().
    # Must be between 1 and 500 characters inclusive.
    if len(reason) < 1 or len(reason) > 500:
        raise ValueError(
            f"decision_dict key 'reason' must be 1–500 characters, "
            f"but got length {len(reason)}"
        )

    # ------------------------------------------------------------------
    # Steps 6–8: APPROVED-only checks
    # ------------------------------------------------------------------
    # The remaining validation steps ONLY apply when decision == "APPROVED".
    # For VETOED and NO_ACTION decisions, validation is complete at this point.
    # This is an early return pattern — if not APPROVED, we're done.
    if decision_dict["decision"] != "APPROVED":
        return

    # ------------------------------------------------------------------
    # Step 6: Check 'position_size' — positive int ≥ 1, booleans rejected
    # ------------------------------------------------------------------
    # PYTHON QUIRK REMINDER: bool is a subclass of int, so we MUST check
    # isinstance(x, bool) BEFORE isinstance(x, int).  If we don't,
    # True (which equals 1) would pass the int check and the ≥ 1 check!
    #
    # In C++, you'd never have this problem because bool and int are
    # separate types.  In Python, this inheritance is a common gotcha.
    if "position_size" not in decision_dict:
        raise ValueError(
            "decision_dict is missing required key 'position_size'"
        )

    pos_size = decision_dict["position_size"]

    # Reject booleans FIRST — before the int check (Python quirk)
    if isinstance(pos_size, bool):
        raise ValueError(
            f"decision_dict key 'position_size' must be a positive integer "
            f"(≥ 1), but got bool"
        )

    # Must be an int (not float — share counts are whole numbers)
    if not isinstance(pos_size, int):
        raise ValueError(
            f"decision_dict key 'position_size' must be a positive integer "
            f"(≥ 1), but got {type(pos_size).__name__}"
        )

    # Must be strictly positive (at least 1 share)
    if pos_size < 1:
        raise ValueError(
            f"decision_dict key 'position_size' must be ≥ 1, "
            f"but got {pos_size}"
        )

    # ------------------------------------------------------------------
    # Step 7: Check 'stop_loss' — positive float > 0.0, booleans rejected
    # ------------------------------------------------------------------
    # Same Python quirk applies: bool is a subclass of int, and int is
    # accepted by isinstance(x, (int, float)), so we reject bool first.
    if "stop_loss" not in decision_dict:
        raise ValueError(
            "decision_dict is missing required key 'stop_loss'"
        )

    stop_loss = decision_dict["stop_loss"]

    # Reject booleans FIRST (Python quirk: bool → int → numeric)
    if isinstance(stop_loss, bool):
        raise ValueError(
            f"decision_dict key 'stop_loss' must be a positive number "
            f"(> 0.0), but got bool"
        )

    # Must be a number (int or float).  We accept both because YAML may
    # parse "150" as int and "150.5" as float — both are valid prices.
    if not isinstance(stop_loss, (int, float)):
        raise ValueError(
            f"decision_dict key 'stop_loss' must be a positive number "
            f"(> 0.0), but got {type(stop_loss).__name__}"
        )

    # Must be strictly positive (> 0.0) — a stop-loss at 0 makes no sense
    if stop_loss <= 0.0:
        raise ValueError(
            f"decision_dict key 'stop_loss' must be > 0.0, "
            f"but got {stop_loss}"
        )

    # ------------------------------------------------------------------
    # Step 8: Check 'symbol' — non-empty string, 1–10 characters
    # ------------------------------------------------------------------
    # Symbol is required for APPROVED decisions because we need to know
    # which ticker to trade.
    if "symbol" not in decision_dict:
        raise ValueError(
            "decision_dict is missing required key 'symbol'; "
            "a symbol is required for order execution"
        )

    symbol = decision_dict["symbol"]

    # Must be a string
    if not isinstance(symbol, str):
        raise ValueError(
            f"decision_dict key 'symbol' must be a non-empty string "
            f"(1–10 chars), but got {type(symbol).__name__}; "
            f"a symbol is required for order execution"
        )

    # Must be between 1 and 10 characters inclusive.
    # Ticker symbols are short — "AAPL" is 4, "BRK.B" is 5.
    # 10 chars is a generous upper bound.
    if len(symbol) < 1 or len(symbol) > 10:
        raise ValueError(
            f"decision_dict key 'symbol' must be 1–10 characters, "
            f"but got length {len(symbol)}; "
            f"a symbol is required for order execution"
        )
