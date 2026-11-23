# orchestrator/safety_check.py
#
# Startup safety check: cross-references all open positions against open
# stop-loss orders to identify any position missing a protective stop-loss.
#
# This runs ONCE on every startup — before the main loop begins — to ensure
# no position is ever held unprotected after a restart or crash.
#
# Key design principles:
#   - Checks ALL positions, including pre-existing ones the Orchestrator
#     did not open (positions without a matching record in the State_File).
#   - Does NOT automatically place stop-loss orders — it only flags loudly.
#   - If the Alpaca API call fails, the exception propagates to the caller
#     which halts startup (bot SHALL NOT begin trading without confirming
#     position safety).
#
# Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7

import logging

# alpaca-py SDK imports for the orders filter request.
# GetOrdersRequest is a Pydantic model that specifies query parameters
# for the GET /v2/orders endpoint.
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus


# Module-level logger — consistent with project conventions.
# Using "orchestrator" so all orchestrator logs share the same logger name.
logger = logging.getLogger("orchestrator")


def run_safety_check(client) -> dict:
    """
    Cross-reference all open positions against open stop-loss orders.

    Algorithm:
      1. Query all open positions via client.get_all_positions().
      2. Query all open orders via client.get_orders(filter=GetOrdersRequest(
             status="open")).
      3. For each position:
         - Determine if it's long (qty > 0) or short (qty < 0).
         - Search open orders for a matching stop-loss:
           type in ("stop", "stop_limit") AND
           symbol matches AND
           side is "sell" for long / "buy" for short.
         - If no match found → log CRITICAL with position details.
      4. Log INFO summary: total positions checked, count unprotected.
      5. Return {"total_positions": N, "unprotected_count": M}.

    If Alpaca API fails (timeout, HTTP error), the exception propagates
    to the caller.  The caller handles it by logging CRITICAL and halting
    startup with a non-zero exit code.

    Args:
        client: Alpaca TradingClient (paper).  Must be an already-initialized
                client instance that has .get_all_positions() and .get_orders()
                methods.

    Returns:
        dict with keys:
          "total_positions"  — int, number of open positions checked.
          "unprotected_count" — int, number of positions missing stop-loss.

    Raises:
        Any exception from the Alpaca API calls (e.g., network timeout,
        HTTP error).  These propagate unmodified to the caller.
    """
    # ------------------------------------------------------------------
    # Step 1: Query all open positions.
    # ------------------------------------------------------------------
    # client.get_all_positions() returns a list of Position objects.
    # Each Position has attributes: symbol, qty, current_price, side, etc.
    positions = client.get_all_positions()

    # ------------------------------------------------------------------
    # Step 2: Query all open orders.
    # ------------------------------------------------------------------
    # GetOrdersRequest is a filter model.  We want all orders with status="open".
    # The Alpaca SDK expects a QueryOrderStatus enum for the status field.
    order_filter = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    orders = client.get_orders(filter=order_filter)

    # ------------------------------------------------------------------
    # Step 3: Check each position for a protective stop-loss.
    # ------------------------------------------------------------------
    unprotected_count = 0
    total_positions = len(positions)

    for position in positions:
        # Determine if position is long or short.
        # Alpaca returns qty as a string (may be fractional).
        qty = float(position.qty)

        # Determine the expected stop-loss side:
        #   - Long position (qty > 0) needs a SELL stop-loss.
        #   - Short position (qty < 0) needs a BUY stop-loss.
        if qty > 0:
            expected_side = "sell"
        elif qty < 0:
            expected_side = "buy"
        else:
            # Zero-quantity position — skip (shouldn't exist but be safe).
            total_positions -= 1
            continue

        # Search orders for a matching protective stop-loss.
        # A stop-loss order matches if:
        #   - type is "stop" or "stop_limit"
        #   - symbol matches the position's symbol
        #   - side matches expected_side
        has_stop_loss = False
        for order in orders:
            # order.type is an enum or string; we compare via string value.
            # order.side is also an enum or string.
            order_type = str(order.type).lower()
            order_side = str(order.side).lower()
            order_symbol = str(order.symbol).upper()
            position_symbol = str(position.symbol).upper()

            if (
                order_type in ("stop", "stop_limit")
                and order_symbol == position_symbol
                and order_side == expected_side
            ):
                has_stop_loss = True
                break  # Found a match — no need to check more orders.

        # If no matching stop-loss was found, log CRITICAL.
        if not has_stop_loss:
            unprotected_count += 1

            # Compute market value: qty * current_price.
            # current_price may be a string from Alpaca.
            try:
                current_price = float(position.current_price)
                market_value = abs(qty) * current_price
            except (TypeError, ValueError, AttributeError):
                market_value = 0.0  # Fallback if price is unavailable.

            # Log CRITICAL with structured event fields.
            # Using the structured format consistent with the design doc.
            logger.critical(
                "Unprotected position detected: "
                f"symbol={position.symbol}, "
                f"quantity={qty}, "
                f"market_value={market_value:.2f}",
                extra={
                    "event": "unprotected_position_detected",
                    "symbol": str(position.symbol),
                    "quantity": qty,
                    "market_value": market_value,
                },
            )

    # ------------------------------------------------------------------
    # Step 4: Log INFO summary.
    # ------------------------------------------------------------------
    logger.info(
        f"Safety check completed: "
        f"total_positions={total_positions}, "
        f"unprotected_count={unprotected_count}",
        extra={
            "event": "safety_check_completed",
            "total_positions": total_positions,
            "unprotected_count": unprotected_count,
        },
    )

    # ------------------------------------------------------------------
    # Step 5: Return summary dict.
    # ------------------------------------------------------------------
    return {
        "total_positions": total_positions,
        "unprotected_count": unprotected_count,
    }
