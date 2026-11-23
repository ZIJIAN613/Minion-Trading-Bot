# test_execution_live.py
#
# Standalone script that exercises the execution module against the real
# Alpaca PAPER trading API.  It constructs a hand-crafted APPROVED BUY
# Decision_Dict and submits it through execute_decision().
#
# PAPER TRADING ONLY — this will attempt to buy 1 share of AAPL on the
# Alpaca paper account.  No real money is at risk.
#
# Usage:
#   python test_execution_live.py
#
# Prerequisites:
#   - .env file with APCA_API_KEY_ID and APCA_API_SECRET_KEY set
#   - Alpaca paper account with sufficient buying power (~$250)
#   - Market hours (or the order will remain "new" until market opens)

import os
import pprint

import pandas as pd

# Ensure we can import from the project root.
# sys.path manipulation is like adding an include directory in C++.
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from execution import execute_decision, load_config


def main():
    # ------------------------------------------------------------------
    # Step 1: Load execution config from execution/config.yaml
    # ------------------------------------------------------------------
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "execution", "config.yaml"
    )
    config = load_config(config_path)
    print(f"Config loaded: timeout={config.fill_timeout_seconds}s, "
          f"poll_interval={config.poll_interval_seconds}s")

    # ------------------------------------------------------------------
    # Step 2: Construct a hand-crafted APPROVED BUY Decision_Dict
    # ------------------------------------------------------------------
    # Using a realistic AAPL price (~$220 as of mid-2025) with a 3%
    # stop-loss below that price.
    current_price_estimate = 220.00
    stop_loss_price = round(current_price_estimate * 0.97, 2)  # 3% below

    decision_dict = {
        "decision": "APPROVED",
        "signal": "BUY",
        "symbol": "AAPL",
        "position_size": 1,              # Buy just 1 share
        "stop_loss": stop_loss_price,    # ~$213.40
        "reason": "Manual integration test of execution module",
        "timestamp": pd.Timestamp.now("UTC"),
    }

    print("\n--- Decision Dict ---")
    pprint.pprint(decision_dict)

    # ------------------------------------------------------------------
    # Step 3: Call execute_decision (uses real Alpaca paper client)
    # ------------------------------------------------------------------
    # Passing client=None so the module creates its own paper client
    # via _create_paper_client() with hard-coded paper=True.
    print("\nSubmitting order to Alpaca paper trading...")
    print(f"  Symbol: {decision_dict['symbol']}")
    print(f"  Quantity: {decision_dict['position_size']}")
    print(f"  Stop-loss: ${stop_loss_price}")
    print(f"  Timeout: {config.fill_timeout_seconds}s")
    print()

    result = execute_decision(decision_dict, config, client=None)

    # ------------------------------------------------------------------
    # Step 4: Print the full Order_Result
    # ------------------------------------------------------------------
    print("\n--- Order Result ---")
    pprint.pprint(result)

    # Quick summary line
    print(f"\nStatus: {result['status']}")
    if result["status"] == "filled":
        print(f"  Fill price: ${result['fill_price']}")
        print(f"  Filled qty: {result['filled_quantity']}")
        print(f"  Entry order: {result['entry_order_id']}")
        print(f"  Stop-loss order: {result['stop_loss_order_id']}")
    elif result["status"] == "error":
        print(f"  Error: {result['reason']}")
    elif result["status"] == "skipped":
        print(f"  Reason: {result['reason']}")


if __name__ == "__main__":
    main()
