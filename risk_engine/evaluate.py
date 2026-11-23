# risk_engine/evaluate.py
#
# Core evaluation logic for the risk engine.
# Contains the single public function evaluate_signal() which applies
# all risk guardrails to a trading signal and returns a Decision_Dict.
#
# DESIGN PRINCIPLE — PURE FUNCTION:
# evaluate_signal() has ZERO side effects. It does not:
#   - Read files (config is loaded separately and passed in)
#   - Access the network
#   - Modify global state
#   - Mutate its input arguments
#
# This makes it fully deterministic and trivially testable.

import math

from risk_engine.config_loader import RiskConfig
from risk_engine.validators import (
    validate_signal_dict,
    validate_current_price,
    validate_account_snapshot,
)


def evaluate_signal(
    signal_dict: dict,
    current_price: float,
    account_snapshot: dict,
    config: RiskConfig,
) -> dict:
    """
    Evaluate a trading signal against risk guardrails.

    This function follows a strict validation-then-evaluation pipeline.
    Validation checks run in a defined order — the first failure raises
    immediately (fail-fast).

    Algorithm:
      Step 1  — Validate signal_dict (TypeError or ValueError)
      Step 2  — Validate current_price (ValueError)
      Step 3  — Validate account_snapshot (TypeError or ValueError)
      Step 4  — HOLD pass-through: return NO_ACTION immediately
      Step 5  — Risk Check #1: Daily loss limit
      Step 6  — Risk Check #2: Max open positions (BUY) or position-held (SELL)
      Step 7  — Risk Check #3: Position sizing (BUY) or full position (SELL)
      Step 8  — Risk Check #4: Stop-loss computation and validation
      Step 9  — Build and return APPROVED Decision_Dict

    Args:
        signal_dict:      Signal from strategy module (Signal_Dict).
        current_price:    Current market price (positive number).
        account_snapshot: Account state dict (equity, cash, open_positions,
                          daily_realized_pnl).
        config:           Validated RiskConfig with risk parameters.

    Returns:
        Decision_Dict with keys: decision, signal, position_size, stop_loss,
        reason, timestamp, and optionally symbol.

    Raises:
        TypeError:  If signal_dict or account_snapshot is not a dict.
        ValueError: If any input validation fails.
    """

    # ------------------------------------------------------------------
    # Step 1: Validate signal_dict
    # ------------------------------------------------------------------
    # Checks (in order): dict type → 'signal' key → 'timestamp' key → 'symbol' key
    # Raises TypeError if not a dict, ValueError if contents are invalid.
    validate_signal_dict(signal_dict)

    # ------------------------------------------------------------------
    # Step 2: Validate current_price
    # ------------------------------------------------------------------
    # Checks: must be int or float (not bool), must be > 0.
    # Raises ValueError on failure.
    validate_current_price(current_price)

    # ------------------------------------------------------------------
    # Step 3: Validate account_snapshot
    # ------------------------------------------------------------------
    # Checks (in order): dict type → equity → cash → open_positions → daily_realized_pnl
    # Raises TypeError if not a dict, ValueError if contents are invalid.
    validate_account_snapshot(account_snapshot)

    # ------------------------------------------------------------------
    # Step 4: HOLD pass-through
    # ------------------------------------------------------------------
    # HOLD signals require no risk evaluation at all — we return immediately
    # with a NO_ACTION Decision_Dict.  This avoids wasting computation on
    # signals that will never result in a trade.
    if signal_dict["signal"] == "HOLD":
        result = {
            "decision": "NO_ACTION",
            "signal": "HOLD",
            "position_size": 0,
            "stop_loss": None,
            "reason": "HOLD signals require no risk evaluation",
            "timestamp": signal_dict["timestamp"],
        }

        # Include symbol only if it was present in the original signal_dict.
        # The `in` operator checks key existence in O(1) for dicts — like
        # std::unordered_map::count() in C++.
        if "symbol" in signal_dict:
            result["symbol"] = signal_dict["symbol"]

        return result

    # ------------------------------------------------------------------
    # Step 5: Risk Check #1 — Daily loss limit
    # ------------------------------------------------------------------
    # If today's realized P&L has reached or exceeded the negative of the
    # configured max daily loss, we veto ALL BUY/SELL signals immediately.
    # This is the FIRST risk check in the defined order, so it takes
    # priority over all subsequent checks (Requirement 9.1).
    #
    # Example: if max_daily_loss = 500 and daily_realized_pnl = -500 (or worse),
    # the condition triggers because -500 <= -500.
    pnl = account_snapshot["daily_realized_pnl"]

    if pnl <= -config.max_daily_loss:
        result = {
            "decision": "VETOED",
            "signal": signal_dict["signal"],
            "position_size": 0,
            "stop_loss": None,
            "reason": (
                f"Daily loss limit reached: realized P&L is {pnl} "
                f"(limit is -{config.max_daily_loss})"
            ),
            "timestamp": signal_dict["timestamp"],
        }

        # Include symbol only if present in the original signal_dict.
        if "symbol" in signal_dict:
            result["symbol"] = signal_dict["symbol"]

        return result

    # ------------------------------------------------------------------
    # Step 6: Risk Check #2 — Max open positions (BUY) / Position-held (SELL)
    # ------------------------------------------------------------------
    # BUY signals are blocked when the portfolio already has the maximum
    # number of open positions.  SELL signals are NEVER blocked by max
    # positions (selling reduces exposure — Requirement 5.3), but SELL
    # signals must verify the position is actually held.

    open_positions = account_snapshot["open_positions"]

    if signal_dict["signal"] == "BUY":
        # --- BUY path: check max open positions limit ---
        if len(open_positions) >= config.max_open_positions:
            result = {
                "decision": "VETOED",
                "signal": signal_dict["signal"],
                "position_size": 0,
                "stop_loss": None,
                "reason": (
                    f"Max open positions limit reached: "
                    f"{len(open_positions)} positions open "
                    f"(limit is {config.max_open_positions})"
                ),
                "timestamp": signal_dict["timestamp"],
            }

            # Include symbol only if present in the original signal_dict.
            if "symbol" in signal_dict:
                result["symbol"] = signal_dict["symbol"]

            return result

    else:
        # --- SELL path: position-held verification ---
        # (At this point the signal must be SELL, since HOLD was already
        # handled in Step 4.)

        # First: symbol must be present for SELL signals so we can look
        # up which position to sell.
        if "symbol" not in signal_dict:
            result = {
                "decision": "VETOED",
                "signal": "SELL",
                "position_size": 0,
                "stop_loss": None,
                "reason": (
                    "A symbol is required for SELL signals to verify "
                    "the position is held"
                ),
                "timestamp": signal_dict["timestamp"],
            }
            return result

        # Second: the symbol must exist in open_positions with size > 0.
        # We iterate through the list looking for a matching position —
        # similar to std::find_if in C++.
        symbol = signal_dict["symbol"]
        held_position = None
        for pos in open_positions:
            if pos["symbol"] == symbol and pos["size"] > 0:
                held_position = pos
                break

        if held_position is None:
            result = {
                "decision": "VETOED",
                "signal": "SELL",
                "position_size": 0,
                "stop_loss": None,
                "reason": (
                    f"Position '{symbol}' is not currently held "
                    f"and cannot be sold"
                ),
                "timestamp": signal_dict["timestamp"],
            }

            # Include symbol in result since it was present in signal_dict.
            if "symbol" in signal_dict:
                result["symbol"] = signal_dict["symbol"]

            return result

        # If we reach here for SELL, held_position is set and valid.
        # Store it at this scope so Step 7 (task 4.8) can use it for
        # position sizing — SELL uses the full held position size.
    # --- End of Step 6 ---
    # If we reach here:
    #   BUY: max positions check passed (room available)
    #   SELL: position-held check passed (held_position is set)

    # ------------------------------------------------------------------
    # Step 7: Risk Check #3 — Position sizing
    # ------------------------------------------------------------------
    # BUY: cap position at the lesser of (equity-percent limit) and
    #       (available cash).  Use math.floor to get whole shares.
    # SELL: close the full held position (size already verified in Step 6).

    if signal_dict["signal"] == "BUY":
        # --- BUY position sizing ---
        equity = account_snapshot["equity"]
        cash = account_snapshot["cash"]

        # Maximum capital this trade is allowed to use, based on the
        # configured percentage of total equity.
        max_allowed_capital = equity * (config.max_position_percent / 100)

        # math.floor() rounds DOWN to the nearest integer — like
        # static_cast<int>(value) truncation in C++, but always toward
        # negative infinity.  We want whole shares only.
        size_from_equity = math.floor(max_allowed_capital / current_price)
        size_from_cash = math.floor(cash / current_price)

        # The actual position size is the minimum of the two constraints.
        # min() is a Python built-in — like std::min in C++.
        position_size = min(size_from_equity, size_from_cash)

        if position_size == 0:
            # Determine which constraint is binding (equity-percent takes
            # priority if both are zero — per Requirement 3.5).
            if size_from_equity == 0:
                reason = (
                    "equity-percent limit prevents a valid position size "
                    "(binding constraint)"
                )
            else:
                reason = "insufficient cash prevents a valid position size"

            result = {
                "decision": "VETOED",
                "signal": signal_dict["signal"],
                "position_size": 0,
                "stop_loss": None,
                "reason": reason,
                "timestamp": signal_dict["timestamp"],
            }

            # Include symbol only if present in the original signal_dict.
            if "symbol" in signal_dict:
                result["symbol"] = signal_dict["symbol"]

            return result

    else:
        # --- SELL position sizing ---
        # Close the full held position.  held_position was already set
        # and validated in Step 6 (SELL path).
        position_size = held_position["size"]

    # ------------------------------------------------------------------
    # Step 8: Risk Check #4 — Stop-loss computation
    # ------------------------------------------------------------------
    # The stop-loss is a price level at which the position MUST be exited
    # to cap losses.  Think of it as a safety net below (BUY) or above
    # (SELL) the entry price.
    #
    # For BUY trades: we place the stop-loss BELOW current_price because
    # we lose money if the price DROPS.
    #   Formula: stop_loss = current_price * (1 - stop_loss_percent / 100)
    #   Example: price=100, percent=2 → stop_loss = 100 * 0.98 = 98.00
    #
    # For SELL trades: we place the stop-loss ABOVE current_price because
    # a short position loses money if the price RISES.
    #   Formula: stop_loss = current_price * (1 + stop_loss_percent / 100)
    #   Example: price=100, percent=2 → stop_loss = 100 * 1.02 = 102.00
    #
    # Edge case (BUY only): if the asset price is very low and
    # stop_loss_percent is very high, the computed stop-loss can be zero
    # or negative — which is not a valid price.  In that case we VETO.
    # For SELL, the formula always produces a value > current_price
    # (because stop_loss_percent is in the range (0, 100) exclusive),
    # so no validity check is needed.
    #
    # round(value, 2) rounds to 2 decimal places — equivalent to
    # std::round(value * 100.0) / 100.0 in C++.

    if signal_dict["signal"] == "BUY":
        # BUY stop-loss: placed below entry price to limit downside.
        stop_loss = round(current_price * (1 - config.stop_loss_percent / 100), 2)

        # Validity check: stop-loss must be a positive price.
        # This can fail for very cheap assets with large stop_loss_percent
        # (e.g., price=0.01, percent=99.5 → stop_loss = 0.01 * 0.005 ≈ 0.00).
        if stop_loss <= 0:
            result = {
                "decision": "VETOED",
                "signal": signal_dict["signal"],
                "position_size": 0,
                "stop_loss": None,
                "reason": "A valid stop-loss price cannot be computed",
                "timestamp": signal_dict["timestamp"],
            }

            # Include symbol only if present in the original signal_dict.
            if "symbol" in signal_dict:
                result["symbol"] = signal_dict["symbol"]

            return result

    else:
        # SELL stop-loss: placed above entry price to limit upside risk
        # on the short position.
        # Since 0 < stop_loss_percent < 100, the multiplier
        # (1 + percent/100) is always in (1.0, 2.0), so the result is
        # always strictly greater than current_price.  No validity check
        # needed — this is guaranteed by the config constraint.
        stop_loss = round(current_price * (1 + config.stop_loss_percent / 100), 2)

    # ------------------------------------------------------------------
    # Step 9: Build and return APPROVED Decision_Dict
    # ------------------------------------------------------------------
    # All four risk checks have passed:
    #   1. Daily loss limit      — OK (not exceeded)
    #   2. Open positions / held — OK (room available or position held)
    #   3. Position sizing       — OK (position_size >= 1)
    #   4. Stop-loss             — OK (valid positive price)
    #
    # We build the final Decision_Dict.  The keys are:
    #   decision      — "APPROVED" (the trade is cleared for execution)
    #   signal        — the original signal ("BUY" or "SELL")
    #   position_size — integer number of shares to trade
    #   stop_loss     — float rounded to 2 decimal places
    #   reason        — human-readable explanation
    #   timestamp     — passed through unmodified from signal_dict
    #   symbol        — OPTIONAL: included only if signal_dict had one

    result = {
        "decision": "APPROVED",
        "signal": signal_dict["signal"],
        "position_size": position_size,
        "stop_loss": stop_loss,
        "reason": "All risk checks passed",
        "timestamp": signal_dict["timestamp"],
    }

    # Include symbol only if it was present in the original signal_dict.
    # This conditional inclusion mirrors the pattern used in Steps 4–8.
    # In C++ terms: we only add the key-value pair to the map if the
    # source map contained that key.
    if "symbol" in signal_dict:
        result["symbol"] = signal_dict["symbol"]

    return result
