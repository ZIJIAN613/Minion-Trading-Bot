# execution/execute.py
#
# Core execution logic for the Minions paper-trading pipeline.
# Contains the public execute_decision() function — the ONLY entry point
# for placing, modifying, or cancelling orders in the entire system.
#
# SAFETY INVARIANT:
# This module is restricted to Alpaca PAPER TRADING only.
# The TradingClient is instantiated with paper=True as a HARD-CODED LITERAL.
# No configuration parameter, environment variable, or code path may route
# orders to a live brokerage account.
#
# DESIGN NOTE:
# Unlike risk_engine/evaluate.py (which is a pure function with zero I/O),
# execute_decision() performs real network I/O (Alpaca API calls).
# After validation, ALL exceptions are caught and returned as Order_Result
# dicts with status="error" — the function never crashes the pipeline.

import logging      # Python standard library — structured event logging
import sys          # sys.stderr — fallback output when logging fails
import time         # time.monotonic(), time.sleep() — for fill polling
from datetime import datetime, timezone  # UTC timestamp fallback

import pandas as pd  # pandas Timestamp — used in Order_Result

# Alpaca SDK imports — the official Python client for the Alpaca trading API.
# These are analogous to including a C++ SDK header.
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce

# Shared credential loader from the data_ingestion module.
# We reuse this to avoid duplicating .env reading logic (Requirement 9.2).
from data_ingestion.credentials import load_credentials

# Local module imports — config and validators from this same package.
from execution.config_loader import ExecutionConfig, load_config
from execution.validators import validate_decision_dict


# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------
# logging.getLogger("execution") returns a logger named "execution".
# All log messages from this module share this namespace — consistent with
# the project convention (Requirement 7.7).
# In C++ terms: this is like getting a named singleton logger instance.
logger = logging.getLogger("execution")


# ---------------------------------------------------------------------------
# Safe logging helper
# ---------------------------------------------------------------------------
# Requirement 7.9: If logging raises an exception, we catch it and write a
# fallback to sys.stderr. Logging must NEVER block order placement.
# This is a helper function that wraps logger calls.

def _safe_log(level: str, message: str, **kwargs) -> None:
    """
    Attempt to log a message; if logging raises, write to stderr instead.

    Args:
        level:   One of "info", "warning", "error" (string, not the constant).
        message: The format string for the log message.
        **kwargs: Key-value pairs included as structured data in the message.

    Returns:
        None — never raises.
    """
    # try/except is Python's exception handling — like try/catch in C++.
    # We catch ALL exceptions (Exception is the base class for most errors)
    # so that a broken logging configuration never stops execution.
    try:
        # Build a structured message by appending key=value pairs.
        # f-strings (f"...") embed expressions inside curly braces — like
        # std::format() in C++20.
        structured_parts = [f"{k}={v!r}" for k, v in kwargs.items()]
        # !r means "use repr()" — gives a quoted, unambiguous representation.
        # ', '.join() concatenates list elements with a separator (like
        # boost::algorithm::join in C++).
        full_message = message
        if structured_parts:
            full_message = f"{message} | {', '.join(structured_parts)}"

        # getattr(obj, name) retrieves an attribute by name at runtime —
        # like a virtual function dispatch by name.  Here we use it to call
        # logger.info(), logger.warning(), or logger.error() dynamically.
        log_func = getattr(logger, level, logger.info)
        log_func(full_message)

    except Exception as exc:
        # Fallback: write to stderr if logging itself breaks.
        # sys.stderr.write() is the lowest-level output that's unlikely to fail.
        try:
            sys.stderr.write(
                f"[LOGGING FALLBACK] level={level} message={message} "
                f"kwargs={kwargs} logging_error={exc}\n"
            )
        except Exception:
            # If even stderr fails, silently continue — nothing else we can do.
            pass


# ---------------------------------------------------------------------------
# _create_paper_client
# ---------------------------------------------------------------------------

def _create_paper_client() -> TradingClient:
    """
    Instantiate Alpaca TradingClient with hard-coded paper=True.

    This function:
      1. Loads credentials via data_ingestion.credentials.load_credentials()
      2. Creates TradingClient(api_key, secret_key, paper=True)
         ^^^ paper=True is a HARD-CODED LITERAL — NEVER change this ^^^
      3. Verifies the base URL contains "paper" (case-insensitive)
         If not → raises RuntimeError (live trading FORBIDDEN)

    Returns:
        A TradingClient instance connected to the paper-trading endpoint.

    Raises:
        EnvironmentError: If credentials are missing (propagated unchanged
                          from load_credentials — Requirement 9.4).
        RuntimeError: If the client's base URL does not contain "paper".
    """
    # Step 1: Load API credentials from shared .env (Requirement 9.1).
    # This call may raise EnvironmentError — we let it propagate unchanged.
    # We do NOT log, print, or include credential VALUES in any output (Req 9.3).
    api_key, secret_key = load_credentials()

    # Step 2: Instantiate the Alpaca paper-trading client.
    # paper=True is a LITERAL — never a variable, never from config (Req 6.1, 6.3).
    client = TradingClient(api_key, secret_key, paper=True)  # NEVER change this — live trading is FORBIDDEN

    # Step 3: Verify the base URL contains "paper" (Requirement 6.2).
    # This is a defense-in-depth check: even if paper=True was somehow
    # ineffective, we catch it here before any order can be placed.
    #
    # The Alpaca SDK stores the base URL as an attribute on the client.
    # We retrieve it and perform a case-insensitive substring check.
    # str.lower() converts to lowercase — like std::tolower() in C++.
    # The `in` operator checks substring membership — like
    # std::string::find() != std::string::npos in C++.
    base_url = str(getattr(client, "_base_url", "") or
                   getattr(client, "base_url", "") or "")

    if "paper" not in base_url.lower():
        raise RuntimeError(
            "Live trading is not permitted. The Alpaca client base URL "
            f"does not contain 'paper': '{base_url}'. "
            "This module is restricted to paper trading ONLY."
        )

    return client


# ---------------------------------------------------------------------------
# execute_decision — public entry point
# ---------------------------------------------------------------------------

def execute_decision(
    decision_dict: dict,
    config: ExecutionConfig,
    client: TradingClient | None = None,
) -> dict:
    """
    Process a Decision_Dict and place the appropriate order(s) via Alpaca.

    This is the module's primary entry point. It accepts a Decision_Dict
    (output of risk_engine.evaluate_signal), validates it, and either:
      - Returns immediately for non-approved decisions (VETOED/NO_ACTION)
      - Submits the appropriate order(s) for APPROVED decisions
      - Polls for fill confirmation
      - Returns a structured Order_Result

    Algorithm:
      Step 1  — Validate decision_dict (TypeError or ValueError propagate)
      Step 2  — Non-approved pass-through: return "skipped" immediately
      Step 3  — HOLD guard: APPROVED+HOLD is malformed, return "skipped"
      Step 4  — Create/reuse Alpaca paper client
      Step 5  — Submit order (bracket for BUY, plain market for SELL)
      Step 6  — Poll for fill confirmation
      Step 7  — Build and return Order_Result

    Args:
        decision_dict: Decision from risk engine (Decision_Dict).
        config:        Validated ExecutionConfig with timeout/poll params.
        client:        Optional pre-created TradingClient (for testing).
                       If None, creates a new paper client internally.

    Returns:
        Order_Result dict with status and details of the execution attempt.
        Never raises after validation passes — all errors are caught and
        returned as Order_Result with status="error".

    Raises:
        TypeError:  If decision_dict is not a dict (from validator).
        ValueError: If decision_dict contents are invalid (from validator).
    """
    # ------------------------------------------------------------------
    # Step 1: Validate the incoming Decision_Dict
    # ------------------------------------------------------------------
    # This call raises TypeError or ValueError on invalid input.
    # We let those exceptions propagate to the caller — they indicate
    # a programming error upstream, not a runtime API failure.
    validate_decision_dict(decision_dict)

    # ------------------------------------------------------------------
    # Step 2: Handle non-approved decisions (VETOED, NO_ACTION)
    # ------------------------------------------------------------------
    # REQUIREMENT 2.3: The Alpaca_Paper_Client SHALL NOT be instantiated
    # or invoked for non-approved decisions.  We return BEFORE any client
    # interaction.
    #
    # .get() is a dict method that returns a default value (here None) if
    # the key doesn't exist — like std::map::find() returning end() in C++.
    # This avoids a KeyError if "symbol" is absent (it's optional for
    # VETOED/NO_ACTION).

    decision = decision_dict["decision"]
    signal = decision_dict["signal"]
    timestamp = decision_dict["timestamp"]
    symbol = decision_dict.get("symbol")  # None if key absent
    reason = decision_dict["reason"]

    if decision == "VETOED":
        # Log structured event for traceability (Requirement 7.6)
        _safe_log(
            "info",
            "Decision skipped",
            event="decision_skipped",
            decision="VETOED",
            signal=signal,
            symbol=symbol,
            reason=reason,
        )

        # Build and return the skipped Order_Result (Requirement 2.1)
        result = {
            "status": "skipped",
            "decision": "VETOED",
            "signal": signal,
            "symbol": symbol,
            "position_size": 0,
            "reason": "Decision was VETOED by risk engine",
            "timestamp": timestamp,
        }

        # -----------------------------------------------------------
        # INVARIANT CHECK (Requirement 2.1):
        # After returning for VETOED, there must be NO code path that
        # could place an order.  This explicit guard uses `if ... raise
        # RuntimeError(...)` (NOT assert) so it is unconditionally
        # enforced regardless of Python optimization flags (-O).
        #
        # NOTE: In Python, `assert` statements are STRIPPED when the
        # interpreter runs with -O (optimize).  That's why we use an
        # explicit if/raise — it can never be silently removed.
        # -----------------------------------------------------------
        if decision == "VETOED" and result["status"] == "skipped":
            # We're about to return — verify no order was placed.
            # Since we haven't created a client or called any API,
            # this is a structural safety net.  If someone later
            # refactors and accidentally adds order logic above this
            # return, the invariant fires.
            return result

        # This line should be unreachable due to the return above,
        # but we include it as a defense-in-depth guard.
        raise RuntimeError(
            "INVARIANT VIOLATION: Attempted to continue execution for a "
            "VETOED decision. No order may be placed for non-approved decisions."
        )

    if decision == "NO_ACTION":
        # Log structured event for traceability (Requirement 7.6)
        _safe_log(
            "info",
            "Decision skipped",
            event="decision_skipped",
            decision="NO_ACTION",
            signal=signal,
            symbol=symbol,
            reason=reason,
        )

        # Build and return the skipped Order_Result (Requirement 2.2)
        return {
            "status": "skipped",
            "decision": "NO_ACTION",
            "signal": signal,
            "symbol": symbol,
            "position_size": 0,
            "reason": "No action required (HOLD signal)",
            "timestamp": timestamp,
        }

    # ------------------------------------------------------------------
    # INVARIANT CHECK: If we reach this point, decision MUST be "APPROVED".
    # This is a defensive check to ensure no unknown decision value can
    # slip through to the order placement logic below.
    # ------------------------------------------------------------------
    if decision != "APPROVED":
        raise RuntimeError(
            f"INVARIANT VIOLATION: Decision '{decision}' is not recognized. "
            f"Only 'APPROVED' decisions may proceed to order placement."
        )

    # ------------------------------------------------------------------
    # Step 3: HOLD guard — APPROVED + HOLD is malformed (Requirement 3.6)
    # ------------------------------------------------------------------
    # An APPROVED decision with a HOLD signal is contradictory — you can't
    # "approve" doing nothing.  This guards against a malformed Decision_Dict.
    if signal == "HOLD":
        _safe_log(
            "info",
            "Decision skipped — APPROVED+HOLD is contradictory",
            event="decision_skipped",
            decision="APPROVED",
            signal="HOLD",
            symbol=symbol,
            reason="HOLD signal cannot be executed",
        )

        return {
            "status": "skipped",
            "decision": "APPROVED",
            "signal": "HOLD",
            "symbol": symbol,
            "position_size": 0,
            "reason": "HOLD signal cannot be executed",
            "timestamp": timestamp,
        }

    # ------------------------------------------------------------------
    # Steps 4–7: Order submission, polling, and Order_Result construction
    # ------------------------------------------------------------------
    # All logic from here on is wrapped in a try/except so that ANY
    # exception is caught and returned as an Order_Result with status="error"
    # (Requirement 12.4).  The function NEVER crashes after validation passes.
    #
    # We track `entry_order_id` outside the try so the except clause can
    # report whether an order was submitted before the error occurred
    # (Requirement 12.7).

    # Extract the fields we need for order placement.
    position_size = decision_dict["position_size"]
    stop_loss = decision_dict["stop_loss"]

    # entry_order_id starts as None — it becomes a string ONLY after the
    # Alpaca API returns a successful order submission.  This lets us
    # distinguish "error before submission" from "error after submission"
    # in the except clause (Requirement 12.7).
    entry_order_id = None

    try:
        # --------------------------------------------------------------
        # Step 4: Create or reuse the Alpaca paper client
        # --------------------------------------------------------------
        # If the caller provided a client (for testing), we use it.
        # Otherwise, we create a new paper-trading client internally.
        # _create_paper_client() enforces paper=True (Requirement 6.1).
        if client is None:
            client = _create_paper_client()

        # --------------------------------------------------------------
        # Step 5: Submit order based on signal
        # --------------------------------------------------------------
        # BUY → bracket order (market entry + stop-loss leg)
        # SELL → plain market sell (no stop-loss, no bracket)
        if signal == "BUY":
            submission_result = _submit_buy_bracket(
                client, symbol, position_size, stop_loss
            )
        else:
            # signal == "SELL" (HOLD was already handled above in Step 3)
            submission_result = _submit_sell_market(
                client, symbol, position_size
            )

        # Extract order IDs and fallback flag from submission result.
        entry_order_id = submission_result["entry_order_id"]
        stop_loss_order_id = submission_result["stop_loss_order_id"]
        bracket_fallback = submission_result.get("bracket_fallback", False)

        # Log the order submission event (Requirement 7.1).
        # We log here in addition to the logging inside the submit helpers
        # to ensure the wiring layer captures the event with decision context.
        _safe_log(
            "info",
            "Order submitted via execute_decision",
            event="order_submitted",
            order_type="bracket" if signal == "BUY" and not bracket_fallback else "market",
            side=signal.lower(),
            symbol=symbol,
            quantity=position_size,
            order_id=entry_order_id,
            timestamp=str(timestamp),
        )

        # --------------------------------------------------------------
        # Step 6: Poll for fill confirmation
        # --------------------------------------------------------------
        # _poll_fill returns a dict with status and fill details.
        # It handles all timing, retries, and cancellation internally.
        poll_result = _poll_fill(client, entry_order_id, config)
        poll_status = poll_result["status"]

        # --------------------------------------------------------------
        # Step 7: Build Order_Result based on poll result
        # --------------------------------------------------------------

        # --- 7a: FILLED (Requirement 11.2) ---
        if poll_status == "filled":
            # Log fill confirmation (Requirement 7.2).
            _safe_log(
                "info",
                "Order filled",
                event="order_filled",
                symbol=symbol,
                filled_quantity=poll_result["filled_quantity"],
                fill_price=poll_result["fill_price"],
                order_id=entry_order_id,
                filled_at=str(poll_result["filled_at"]),
            )

            # Build the filled Order_Result.
            result = {
                "status": "filled",
                "decision": "APPROVED",
                "signal": signal,
                "symbol": symbol,
                "position_size": position_size,
                "reason": f"Order filled: {poll_result['filled_quantity']} shares at {poll_result['fill_price']}",
                "timestamp": timestamp,
                "entry_order_id": entry_order_id,
                "stop_loss_order_id": stop_loss_order_id,
                "fill_price": poll_result["fill_price"],
                "filled_quantity": poll_result["filled_quantity"],
                "filled_at": poll_result["filled_at"],
            }

            # Include bracket_fallback if it was used (Requirement 4.5).
            if bracket_fallback:
                result["bracket_fallback"] = True

            return result

        # --- 7b: PARTIAL FILL (Requirement 11.5) ---
        if poll_status == "partial_fill":
            filled_qty = poll_result["filled_quantity"]
            cancelled_qty = poll_result.get("cancelled_quantity", 0)

            # Log partial fill acceptance (Requirement 7.3).
            _safe_log(
                "info",
                "Partial fill accepted",
                event="partial_fill_accepted",
                symbol=symbol,
                filled_quantity=filled_qty,
                cancelled_quantity=cancelled_qty,
                order_id=entry_order_id,
                timeout_seconds=config.fill_timeout_seconds,
            )

            # For BUY signals: verify stop-loss quantity matches filled qty.
            # If not, cancel and resubmit the stop-loss for the correct qty.
            # (Requirement 5.3 — stop-loss quantity correction for partial fills)
            if signal == "BUY" and stop_loss_order_id is not None:
                try:
                    # Check current stop-loss order to see its quantity.
                    sl_order = client.get_order_by_id(stop_loss_order_id)
                    sl_qty = int(float(getattr(sl_order, "qty", 0) or 0))

                    if sl_qty != filled_qty:
                        # Cancel the existing stop-loss and resubmit with
                        # the correct filled quantity.
                        client.cancel_order_by_id(stop_loss_order_id)

                        # Submit a new stop order for the correct quantity.
                        corrected_stop = StopOrderRequest(
                            symbol=symbol,
                            qty=filled_qty,
                            side=OrderSide.SELL,
                            stop_price=stop_loss,
                            time_in_force=TimeInForce.GTC,
                        )
                        new_sl_order = client.submit_order(corrected_stop)
                        stop_loss_order_id = str(new_sl_order.id)

                        _safe_log(
                            "info",
                            "Stop-loss resubmitted for partial fill quantity",
                            event="stop_loss_corrected",
                            order_id=entry_order_id,
                            new_stop_loss_order_id=stop_loss_order_id,
                            corrected_quantity=filled_qty,
                        )

                except Exception as sl_exc:
                    # Log WARNING but do NOT block the return (Requirement 5.3).
                    _safe_log(
                        "warning",
                        "Failed to correct stop-loss quantity for partial fill",
                        event="stop_loss_correction_failed",
                        order_id=entry_order_id,
                        stop_loss_order_id=stop_loss_order_id,
                        error_type=type(sl_exc).__name__,
                        error_message=str(sl_exc)[:500],
                    )

            # Build the partial_fill Order_Result.
            result = {
                "status": "partial_fill",
                "decision": "APPROVED",
                "signal": signal,
                "symbol": symbol,
                "position_size": position_size,
                "reason": f"Partial fill: {filled_qty} shares filled, {cancelled_qty} cancelled after timeout",
                "timestamp": timestamp,
                "entry_order_id": entry_order_id,
                "stop_loss_order_id": stop_loss_order_id,
                "fill_price": poll_result["fill_price"],
                "filled_quantity": filled_qty,
                "filled_at": poll_result["filled_at"],
                "cancelled_quantity": cancelled_qty,
            }

            # Include bracket_fallback if it was used.
            if bracket_fallback:
                result["bracket_fallback"] = True

            return result

        # --- 7c: CANCELLED / EXPIRED / REJECTED (Requirement 11.6) ---
        if poll_status in ("cancelled", "expired", "rejected"):
            # Log terminal state at WARNING level (Requirement 7.4).
            _safe_log(
                "warning",
                "Order reached terminal state without fill",
                event="order_terminal",
                symbol=symbol,
                order_id=entry_order_id,
                terminal_status=poll_status,
                reason=None,
            )

            # For BUY signals: verify stop-loss is also cancelled
            # (Requirement 5.4 — clean up linked stop-loss on terminal).
            if signal == "BUY" and stop_loss_order_id is not None:
                try:
                    sl_order = client.get_order_by_id(stop_loss_order_id)
                    sl_status = str(getattr(sl_order, "status", "")).lower()
                    # If the stop-loss is still active, cancel it.
                    if sl_status not in ("cancelled", "expired", "rejected", "filled"):
                        client.cancel_order_by_id(stop_loss_order_id)
                except Exception as sl_cancel_exc:
                    _safe_log(
                        "warning",
                        "Failed to cancel stop-loss after entry terminal state",
                        event="stop_loss_cancel_failed",
                        order_id=entry_order_id,
                        stop_loss_order_id=stop_loss_order_id,
                        error_type=type(sl_cancel_exc).__name__,
                        error_message=str(sl_cancel_exc)[:500],
                    )

            result = {
                "status": poll_status,
                "decision": "APPROVED",
                "signal": signal,
                "symbol": symbol,
                "position_size": position_size,
                "reason": f"Order {poll_status}",
                "timestamp": timestamp,
                "entry_order_id": entry_order_id,
            }

            if bracket_fallback:
                result["bracket_fallback"] = True

            return result

        # --- 7d: TIMEOUT (Requirement 11.6) ---
        # poll_status == "timeout" — order did not reach terminal state
        # within fill_timeout_seconds.
        _safe_log(
            "warning",
            "Order timed out without reaching terminal state",
            event="order_terminal",
            symbol=symbol,
            order_id=entry_order_id,
            terminal_status="timeout",
            timeout_seconds=config.fill_timeout_seconds,
        )

        result = {
            "status": "timeout",
            "decision": "APPROVED",
            "signal": signal,
            "symbol": symbol,
            "position_size": position_size,
            "reason": f"Order timed out after {config.fill_timeout_seconds}s",
            "timestamp": timestamp,
            "entry_order_id": entry_order_id,
        }

        if bracket_fallback:
            result["bracket_fallback"] = True

        return result

    except Exception as exc:
        # --------------------------------------------------------------
        # Error wrapping (Requirement 12.4)
        # --------------------------------------------------------------
        # ANY exception that occurs after validation passes is caught here.
        # We log an ERROR event and return an Order_Result with status="error".
        # The function NEVER propagates exceptions to the caller after
        # validation — it returns structured results instead.

        # Log the error (Requirement 7.5).
        _safe_log(
            "error",
            "API error during order execution",
            event="api_error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
            symbol=symbol,
            order_id=entry_order_id,
        )

        # Build the error Order_Result (Requirement 12.4, 12.7).
        # entry_order_id is None if the error happened before submission,
        # or the actual Alpaca order ID if it happened after.
        #
        # The reason includes BOTH the exception class name AND the message
        # (Requirement 12.2: "including the exception class name").
        # We truncate to 500 chars total to satisfy Requirement 11.1
        # (reason must be 1–500 characters).
        error_reason = f"{type(exc).__name__}: {str(exc)}"
        # Slice to enforce the 500-character upper bound.
        # In C++ terms: this is like str.substr(0, 500).
        if len(error_reason) > 500:
            error_reason = error_reason[:500]

        return {
            "status": "error",
            "decision": "APPROVED",
            "signal": signal,
            "symbol": symbol,
            "position_size": position_size,
            "reason": error_reason,
            "timestamp": timestamp,
            "entry_order_id": entry_order_id,
            "error_type": type(exc).__name__,
        }


# ---------------------------------------------------------------------------
# _submit_buy_bracket — bracket order submission with fallback
# ---------------------------------------------------------------------------
# Requirement 3.1: BUY signals submit bracket order (market entry + stop-loss)
# Requirement 4.1: Stop-loss is the stop-loss leg of a bracket order
# Requirement 4.3: If bracket rejected, fall back to plain market + separate stop


def _submit_buy_bracket(
    client: TradingClient,
    symbol: str,
    quantity: int,
    stop_loss_price: float,
) -> dict:
    """
    Submit a market buy order, wait for fill, then submit a separate GTC
    stop-loss sell order for the filled quantity.

    WHY NOT OTO or BRACKET:
      - OrderClass.BRACKET requires both stop-loss AND take-profit legs.
        We only want a stop-loss.
      - OrderClass.OTO attaches the child with the parent's time_in_force.
        Since our entry is time_in_force=DAY (market order for immediate
        fill), the child stop-loss ALSO becomes DAY — and Alpaca cancels
        it at market close.  This leaves the position unprotected overnight.

    SOLUTION: Two separate orders.
      1. Plain market buy (time_in_force=DAY) for immediate execution.
      2. Wait for entry to fill (shares confirmed held).
      3. Standalone stop sell order (time_in_force=GTC) — this persists
         until triggered or explicitly cancelled, protecting the position
         indefinitely.

    This two-step approach ensures the stop-loss is always GTC regardless
    of the entry order's time_in_force.

    Args:
        client:          Alpaca TradingClient (paper only).
        symbol:          Ticker to buy (e.g., "AAPL").
        quantity:        Number of shares to buy (positive int ≥ 1).
        stop_loss_price: Price at which the stop-loss sell triggers.

    Returns:
        dict with keys:
          - entry_order_id:     str — Alpaca order ID for the entry buy.
          - stop_loss_order_id: str or None — Alpaca order ID for the GTC stop.
          - bracket_fallback:   bool — always False (this IS the primary path).

    Raises:
        Exception: If the stop-loss submission fails (after entry is already
                   placed), the exception propagates to the caller so it can
                   log ERROR and return an error Order_Result.
    """
    # ------------------------------------------------------------------
    # Step 1: Submit plain market buy order
    # ------------------------------------------------------------------
    buy_request = MarketOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,  # Execute during market hours.
        # No order_class — simple market order, no linked legs.
    )

    # This may raise if submission fails — let it propagate to caller.
    entry_order = client.submit_order(buy_request)
    entry_order_id = str(entry_order.id)

    _safe_log(
        "info",
        "Market buy order submitted",
        event="order_submitted",
        order_type="market",
        side="buy",
        symbol=symbol,
        quantity=quantity,
        entry_order_id=entry_order_id,
    )

    # ------------------------------------------------------------------
    # Step 2: Poll until the entry order fills
    # ------------------------------------------------------------------
    # We must confirm shares are actually held before placing the stop-loss.
    # If we submitted the stop-loss immediately (before fill), and the entry
    # was then rejected, we'd have an orphaned stop-loss on no position.
    #
    # time.sleep() pauses execution — like std::this_thread::sleep_for().
    max_wait_seconds = 30
    poll_interval = 1.0
    elapsed = 0.0
    filled_qty = 0

    while elapsed < max_wait_seconds:
        current_order = client.get_order_by_id(entry_order_id)
        status = str(getattr(current_order, "status", "")).lower()

        if status in ("filled", "partially_filled"):
            # Shares are confirmed held — we can now place the stop.
            filled_qty = int(
                float(getattr(current_order, "filled_qty", 0) or 0)
            )
            break

        # If the entry was rejected/cancelled/expired before filling,
        # we cannot place a stop-loss.  Return without one.
        if status in ("cancelled", "expired", "rejected"):
            _safe_log(
                "warning",
                "Entry order reached terminal state without fill",
                event="order_terminal",
                symbol=symbol,
                entry_order_id=entry_order_id,
                terminal_status=status,
            )
            return {
                "entry_order_id": entry_order_id,
                "stop_loss_order_id": None,
                "bracket_fallback": False,
            }

        time.sleep(poll_interval)
        elapsed += poll_interval

    # If we timed out waiting for fill, return without stop-loss.
    if filled_qty == 0:
        _safe_log(
            "warning",
            "Entry order did not fill within submission timeout",
            event="entry_fill_timeout",
            symbol=symbol,
            entry_order_id=entry_order_id,
            max_wait_seconds=max_wait_seconds,
        )
        return {
            "entry_order_id": entry_order_id,
            "stop_loss_order_id": None,
            "bracket_fallback": False,
        }

    # ------------------------------------------------------------------
    # Step 3: Submit a separate GTC stop-loss sell order
    # ------------------------------------------------------------------
    # StopOrderRequest creates a standalone stop order (not linked to
    # any parent).  time_in_force=GTC means "good till cancelled" — the
    # stop remains active indefinitely until triggered or explicitly
    # cancelled.  This is the KEY difference from OTO (which inherits
    # the parent's DAY and gets cancelled at market close).
    stop_request = StopOrderRequest(
        symbol=symbol,
        qty=filled_qty,
        side=OrderSide.SELL,
        stop_price=stop_loss_price,
        time_in_force=TimeInForce.GTC,  # Persists overnight — CRITICAL
    )

    # If this raises, the caller catches it, logs ERROR, and returns
    # an error Order_Result indicating "entry placed but stop-loss failed".
    stop_order = client.submit_order(stop_request)
    stop_loss_order_id = str(stop_order.id)

    _safe_log(
        "info",
        "GTC stop-loss order submitted",
        event="order_submitted",
        order_type="stop",
        side="sell",
        symbol=symbol,
        quantity=filled_qty,
        stop_loss_order_id=stop_loss_order_id,
        stop_loss_price=stop_loss_price,
        time_in_force="gtc",
    )

    return {
        "entry_order_id": entry_order_id,
        "stop_loss_order_id": stop_loss_order_id,
        "bracket_fallback": False,
    }


# ---------------------------------------------------------------------------
# _submit_sell_market — plain market sell (no stop-loss, no bracket)
# ---------------------------------------------------------------------------
# Requirement 3.2: SELL signals submit plain market sell order.
# Requirement 3.7: stop_loss value is IGNORED for SELL signals.
# Requirement 4.7: No stop-loss order is submitted for SELL.


def _submit_sell_market(
    client: TradingClient,
    symbol: str,
    quantity: int,
) -> dict:
    """
    Submit a plain market sell order — no stop-loss, no bracket.

    SELL signals close existing positions.  Attaching a stop-loss would
    create a buy-side exit that could re-enter the position, which is
    not desired.  Therefore SELL is always a simple market order.

    Args:
        client:   Alpaca TradingClient (paper only).
        symbol:   Ticker to sell (e.g., "AAPL").
        quantity: Number of shares to sell (positive int ≥ 1).

    Returns:
        dict with keys:
          - entry_order_id:     str — Alpaca order ID for the sell.
          - stop_loss_order_id: None — always None for SELL.
          - bracket_fallback:   False — never applicable for SELL.
    """
    # Build a simple market sell order request.
    # No order_class, no stop_loss — just a plain market sell.
    sell_request = MarketOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,   # Valid during market hours only.
        # No order_class — plain/simple order (not bracket).
        # No stop_loss — SELL closes a position, no protective exit needed.
    )

    # Submit the order to Alpaca.  On failure, exception propagates
    # to the caller (execute_decision's error wrapper).
    order = client.submit_order(sell_request)

    # Extract the order ID — str(UUID) gives a string representation.
    entry_order_id = str(order.id)

    # Log the successful sell submission (Requirement 7.1).
    _safe_log(
        "info",
        "Market sell order submitted",
        event="order_submitted",
        order_type="market",
        side="sell",
        symbol=symbol,
        quantity=quantity,
        entry_order_id=entry_order_id,
    )

    return {
        "entry_order_id": entry_order_id,
        "stop_loss_order_id": None,       # SELL has no stop-loss (Req 4.7).
        "bracket_fallback": False,         # Not applicable for SELL.
    }


# ---------------------------------------------------------------------------
# _poll_fill — poll Alpaca until terminal state or timeout
# ---------------------------------------------------------------------------
# Requirement 5.1: Poll at config.poll_interval_seconds intervals.
# Requirement 5.2: Return fill details when status == "filled".
# Requirement 5.3: Accept partial fill at timeout, cancel remaining.
# Requirement 5.4: Return terminal status for cancelled/expired/rejected.
# Requirement 5.5: Final check after timeout; cancel if still non-terminal.
# Requirement 5.6: Include filled_at timestamp (UTC-aware); fallback to now(UTC).
# Requirement 5.7: Only a terminal state counts as completed.
# Requirement 12.3: Re-query once on network error during polling.
# Requirement 12.6: Use re-queried state (not stale local state).


def _poll_fill(
    client: TradingClient,
    order_id: str,
    config: ExecutionConfig,
) -> dict:
    """
    Poll Alpaca for order status until a terminal state or timeout.

    This function implements a polling loop that checks the order's current
    status at regular intervals, respecting a total time budget.

    Terminal states (polling stops immediately):
        "filled"           — order completely filled
        "partially_filled" — at timeout, accept what's filled, cancel rest
        "cancelled"        — order was cancelled (by broker or user)
        "expired"          — order expired (e.g., end of day)
        "rejected"         — order was rejected by the exchange

    Algorithm (see design doc for detailed timing):
      1. Record start_time = time.monotonic()
      2. Loop:
         a. Query order status by order_id.
         b. If status is terminal → return immediately.
         c. Compute elapsed = time.monotonic() - start_time.
         d. If elapsed >= fill_timeout_seconds → break (go to final check).
         e. Compute remaining = fill_timeout_seconds - elapsed.
         f. sleep_duration = min(poll_interval_seconds, remaining).
            ^^^ Clamp: never sleep past the timeout budget.
         g. time.sleep(sleep_duration)
         h. Continue loop.
      3. Final check: Query one last time.
         - If now terminal → return that status.
         - If still non-terminal → cancel order → return status="timeout".

    TIMING NOTE (for C++ developers):
        time.monotonic() returns a float representing seconds from an
        arbitrary epoch.  It's guaranteed to never go backwards — like
        std::chrono::steady_clock in C++.  We use it instead of
        time.time() because wall-clock time can jump (NTP adjustments,
        daylight saving, etc.), which would break our elapsed calculation.

    NETWORK ERROR HANDLING (Requirement 12.3):
        If a network error occurs during a poll query, we re-query ONCE
        by the same order_id.  If the re-query also fails, we break out
        of the loop and proceed to the final check.

    Args:
        client:   Alpaca TradingClient (paper only).
        order_id: The Alpaca order ID string to poll.
        config:   ExecutionConfig with fill_timeout_seconds and
                  poll_interval_seconds.

    Returns:
        dict with keys:
          - status: str — one of "filled", "partial_fill", "cancelled",
                    "expired", "rejected", "timeout"
          - fill_price: float (present for "filled" / "partial_fill")
          - filled_quantity: int (present for "filled" / "partial_fill")
          - filled_at: pd.Timestamp (UTC-aware, present for "filled" / "partial_fill")
          - cancelled_quantity: int (present for "partial_fill" — unfilled remainder)
    """
    # ------------------------------------------------------------------
    # Define the set of terminal states.
    # ------------------------------------------------------------------
    # A frozenset is an immutable set — like a const std::unordered_set
    # in C++.  Using a set gives O(1) membership checks with `in`.
    TERMINAL_STATES = frozenset([
        "filled",
        "partially_filled",
        "cancelled",
        "expired",
        "rejected",
    ])

    # ------------------------------------------------------------------
    # Helper: query order with single-retry on network error
    # ------------------------------------------------------------------
    # This inner function encapsulates the network-error retry logic
    # (Requirement 12.3).  It returns the order object on success, or
    # raises the exception if both attempts fail.
    #
    # "def inside def" — In Python, functions can be defined inside other
    # functions.  The inner function has access to variables from the outer
    # scope (like `client` and `order_id`).  This is called a "closure" —
    # similar to a lambda capturing variables in C++.

    def _query_order_with_retry() -> object:
        """
        Query order status; on network error, retry exactly once.

        Returns:
            The Alpaca order object from client.get_order_by_id().

        Raises:
            Exception: If both the original query and the re-query fail.
        """
        try:
            return client.get_order_by_id(order_id)
        except Exception as first_error:
            # Network error on first attempt — re-query once (Req 12.3).
            _safe_log(
                "warning",
                "Network error during poll, re-querying once",
                event="poll_network_error",
                order_id=order_id,
                error_type=type(first_error).__name__,
                error_message=str(first_error)[:500],
            )
            try:
                return client.get_order_by_id(order_id)
            except Exception as second_error:
                # Both attempts failed — raise the second error so the
                # caller can decide what to do.
                _safe_log(
                    "error",
                    "Re-query also failed during poll",
                    event="poll_requery_failed",
                    order_id=order_id,
                    error_type=type(second_error).__name__,
                    error_message=str(second_error)[:500],
                )
                raise second_error from first_error

    # ------------------------------------------------------------------
    # Helper: extract result dict from an Alpaca order object
    # ------------------------------------------------------------------
    # Centralizes the logic for building the return dict from an order,
    # so we don't repeat it in multiple places.

    def _build_result_from_order(order) -> dict:
        """
        Build the poll result dict from an Alpaca order object.

        Handles:
          - "filled"            → status="filled", with fill details
          - "partially_filled"  → status="partial_fill", with fill details
                                  + cancel remaining + cancelled_quantity
          - Other terminal      → just status string

        Returns:
            dict with status, fill_price, filled_quantity, filled_at, etc.
        """
        status_str = str(getattr(order, "status", "")).lower()

        # --- FILLED (Requirement 5.2) ---
        if status_str == "filled":
            # Extract fill price — Alpaca may return it as a string.
            # float() converts string or numeric to a Python float.
            fill_price = float(getattr(order, "filled_avg_price", 0) or 0)

            # Extract filled quantity — may also be a string from the API.
            filled_quantity = int(
                float(getattr(order, "filled_qty", 0) or 0)
            )

            # Extract filled_at timestamp (Requirement 5.6).
            # Alpaca may provide this as a datetime or None.
            raw_filled_at = getattr(order, "filled_at", None)

            if raw_filled_at is not None:
                # Convert to pandas Timestamp (UTC-aware).
                # pd.Timestamp() accepts datetime objects, strings, etc.
                #
                # IMPORTANT: If raw_filled_at is already timezone-aware
                # (has .tzinfo set), passing tz="UTC" as well would raise
                # ValueError.  So we first convert WITHOUT specifying tz,
                # then use tz_localize (if naive) or tz_convert (if aware)
                # to ensure UTC.  This is a common pandas gotcha.
                ts = pd.Timestamp(raw_filled_at)
                if ts.tzinfo is None:
                    # Naive timestamp — assume UTC and localize.
                    filled_at = ts.tz_localize("UTC")
                else:
                    # Already timezone-aware — convert to UTC.
                    filled_at = ts.tz_convert("UTC")
            else:
                # Fallback: use current UTC time (Requirement 5.6).
                # pd.Timestamp.now(tz="UTC") creates a timezone-aware
                # Timestamp representing the current moment in UTC.
                # We use this instead of datetime.now() to avoid the
                # pandas tz gotcha described above.
                filled_at = pd.Timestamp.now(tz="UTC")

            return {
                "status": "filled",
                "fill_price": fill_price,
                "filled_quantity": filled_quantity,
                "filled_at": filled_at,
            }

        # --- PARTIALLY FILLED (Requirement 5.3) ---
        # This is reached when the order is partially_filled at timeout.
        # We accept the filled portion and cancel the remainder.
        if status_str == "partially_filled":
            fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
            filled_quantity = int(
                float(getattr(order, "filled_qty", 0) or 0)
            )

            # Compute cancelled_quantity = total requested - filled.
            # order.qty is the original requested quantity.
            total_qty = int(float(getattr(order, "qty", 0) or 0))
            cancelled_quantity = total_qty - filled_quantity

            # Cancel the remaining unfilled portion (Requirement 5.3).
            try:
                client.cancel_order_by_id(order_id)
            except Exception as cancel_exc:
                # Log but don't fail — the partial fill is still valid.
                _safe_log(
                    "warning",
                    "Failed to cancel remaining portion of partial fill",
                    event="cancel_partial_failed",
                    order_id=order_id,
                    error_type=type(cancel_exc).__name__,
                    error_message=str(cancel_exc)[:500],
                )

            # filled_at timestamp — same safe conversion as filled path.
            raw_filled_at = getattr(order, "filled_at", None)
            if raw_filled_at is not None:
                ts = pd.Timestamp(raw_filled_at)
                if ts.tzinfo is None:
                    filled_at = ts.tz_localize("UTC")
                else:
                    filled_at = ts.tz_convert("UTC")
            else:
                filled_at = pd.Timestamp.now(tz="UTC")

            return {
                "status": "partial_fill",
                "fill_price": fill_price,
                "filled_quantity": filled_quantity,
                "filled_at": filled_at,
                "cancelled_quantity": cancelled_quantity,
            }

        # --- OTHER TERMINAL STATES (Requirement 5.4) ---
        # "cancelled", "expired", "rejected" — no fill details to report.
        return {
            "status": status_str,
        }

    # ==================================================================
    # MAIN POLLING LOOP
    # ==================================================================

    # Step 1: Record start time using monotonic clock.
    # time.monotonic() — like std::chrono::steady_clock::now() in C++.
    # It returns seconds (as a float) since an arbitrary reference point.
    # Key property: it NEVER goes backwards, even if system time is adjusted.
    start_time = time.monotonic()

    while True:
        # Step 2a: Query the order status.
        try:
            order = _query_order_with_retry()
        except Exception:
            # Both query attempts failed (Requirement 12.3).
            # Break out of loop → proceed to final check below.
            break

        # Step 2b: Check if status is terminal.
        status_str = str(getattr(order, "status", "")).lower()

        if status_str in TERMINAL_STATES:
            # Terminal state reached — return immediately.
            return _build_result_from_order(order)

        # Step 2c: Compute elapsed time since polling started.
        elapsed = time.monotonic() - start_time

        # Step 2d: Check if we've exceeded the timeout budget.
        if elapsed >= config.fill_timeout_seconds:
            # Timeout exceeded — break out to do the final check.
            break

        # Step 2e: Compute remaining time budget.
        remaining = config.fill_timeout_seconds - elapsed

        # Step 2f: Clamp sleep duration to remaining budget.
        # min() picks the smaller of two values — ensures we NEVER
        # sleep past the timeout.  Example: if only 0.3s remain but
        # poll_interval is 1.0s, we sleep only 0.3s.
        sleep_duration = min(config.poll_interval_seconds, remaining)

        # Step 2g: Sleep before next poll iteration.
        # time.sleep(seconds) pauses the current thread — like
        # std::this_thread::sleep_for(std::chrono::duration) in C++.
        time.sleep(sleep_duration)

        # Step 2h: Continue loop (next iteration queries status again).

    # ==================================================================
    # FINAL CHECK (Requirement 5.5)
    # ==================================================================
    # We arrive here either because:
    #   - The timeout was exceeded (elapsed >= fill_timeout_seconds), OR
    #   - A network error caused both query attempts to fail.
    #
    # We query the order ONE LAST TIME to get the most current state.
    # If it's now terminal, we return that state.
    # If still non-terminal, we cancel the order and return "timeout".

    try:
        final_order = client.get_order_by_id(order_id)
        final_status = str(getattr(final_order, "status", "")).lower()

        if final_status in TERMINAL_STATES:
            # The order reached a terminal state between our last check
            # and this final query — return it.
            return _build_result_from_order(final_order)

        # Still non-terminal — cancel the order (Requirement 5.5).
        try:
            client.cancel_order_by_id(order_id)
        except Exception as cancel_exc:
            # Log the cancel failure, but still return timeout.
            _safe_log(
                "warning",
                "Failed to cancel order after timeout",
                event="timeout_cancel_failed",
                order_id=order_id,
                error_type=type(cancel_exc).__name__,
                error_message=str(cancel_exc)[:500],
            )

        return {"status": "timeout"}

    except Exception as final_exc:
        # Even the final check failed — we can't determine the order state.
        # Return timeout since we've exhausted our time budget.
        _safe_log(
            "error",
            "Final status check failed after timeout",
            event="final_check_failed",
            order_id=order_id,
            error_type=type(final_exc).__name__,
            error_message=str(final_exc)[:500],
        )
        return {"status": "timeout"}
