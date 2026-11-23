"""
scratch/verify_risk_approval.py
================================
Verifies the full signal → risk evaluation chain produces APPROVED
(not VETOED) with the real Alpaca paper account state and the new
short=7/long=15 strategy config.

Usage (from project root):
    python scratch/verify_risk_approval.py
"""

import sys
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to sys.path so module imports work.
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from data_ingestion.credentials import load_credentials
from data_ingestion import RestClient

from strategy import compute_signal, StrategyConfig

from risk_engine import evaluate_signal, load_config as load_risk_config

# Alpaca SDK — we need TradingClient to fetch account state and positions.
from alpaca.trading.client import TradingClient

from orchestrator.state_manager import format_account_snapshot


def main():
    # ------------------------------------------------------------------
    # 1. Load credentials and create clients.
    # ------------------------------------------------------------------
    api_key, secret_key = load_credentials()
    print("Credentials loaded.")

    # Paper trading client — same as orchestrator uses.
    trading_client = TradingClient(api_key, secret_key, paper=True)

    # Market data client (read-only).
    rest_client = RestClient(api_key, secret_key)

    # ------------------------------------------------------------------
    # 2. Fetch real account state and build account_snapshot.
    # ------------------------------------------------------------------
    # Alpaca paper API can return 503 transiently — retry up to 3 times.
    for attempt in range(1, 4):
        try:
            account = trading_client.get_account()
            positions = trading_client.get_all_positions()
            break
        except Exception as exc:
            if attempt < 3:
                print(f"  Attempt {attempt} failed ({exc}), retrying in 5s...")
                time.sleep(5)
            else:
                print(f"  All 3 attempts failed. Last error: {exc}")
                print("  Alpaca paper trading API appears to be down (503).")
                print("  Try again in a few minutes.")
                return

    print(f"\n--- Account State ---")
    print(f"  Equity:       ${float(account.equity):,.2f}")
    print(f"  Cash:         ${float(account.cash):,.2f}")
    print(f"  Last Equity:  ${float(account.last_equity):,.2f}")
    print(f"  Daily P&L:    ${float(account.equity) - float(account.last_equity):,.2f}")
    print(f"  Positions:    {len(positions)}")
    for pos in positions:
        print(f"    {pos.symbol}: {pos.qty} shares @ ${float(pos.current_price):,.2f}")

    # format_account_snapshot() transforms the raw Alpaca objects into the
    # dict format that risk_engine.evaluate_signal() expects.
    account_snapshot = format_account_snapshot(account, positions)
    print(f"\n--- Formatted account_snapshot (Risk Engine input) ---")
    print(json.dumps(account_snapshot, indent=2))

    # ------------------------------------------------------------------
    # 3. Fetch AAPL bars and compute signal with short=7/long=15.
    # ------------------------------------------------------------------
    symbol = "AAPL"
    config = StrategyConfig(short_window=7, long_window=15)

    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (
        datetime.now(timezone.utc) - timedelta(days=120)
    ).strftime("%Y-%m-%d")

    print(f"\nFetching {symbol} daily bars ({start_date} to {end_date})...")
    df = rest_client.get_historical_bars(symbol, "1d", start_date, end_date)
    print(f"Received {len(df)} bars. Last close: ${df['close'].iloc[-1]:,.2f}")

    signal_dict = compute_signal(df, config, symbol=symbol)
    print(f"\n--- Signal ---")
    print(json.dumps(
        {k: str(v) if hasattr(v, 'isoformat') else v for k, v in signal_dict.items()},
        indent=2,
    ))

    if signal_dict["signal"] == "HOLD":
        print("\n⚠️  Signal is HOLD — risk evaluation will return NO_ACTION.")
        print("   The short=7/long=15 config may no longer cross on the latest bar.")
        return

    # ------------------------------------------------------------------
    # 4. Run risk_engine.evaluate_signal() with real data.
    # ------------------------------------------------------------------
    # current_price = last close from the bars (same as orchestrator uses
    # when there's no live quote — the latest bar close).
    current_price = float(df["close"].iloc[-1])

    risk_config = load_risk_config("risk_engine/config.yaml")

    decision = evaluate_signal(signal_dict, current_price, account_snapshot, risk_config)

    print(f"\n--- Risk Engine Decision ---")
    print(json.dumps(
        {k: str(v) if hasattr(v, 'isoformat') else v for k, v in decision.items()},
        indent=2,
    ))

    # ------------------------------------------------------------------
    # 5. Summary verdict.
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    if decision["decision"] == "APPROVED":
        print(f"✅ APPROVED — BUY {decision['position_size']} shares of {symbol}")
        print(f"   Stop-loss: ${decision['stop_loss']}")
        print(f"   This WILL reach the deferred-execution path.")
    elif decision["decision"] == "VETOED":
        print(f"❌ VETOED — {decision['reason']}")
        print(f"   This will NOT reach the deferred-execution path.")
    else:
        print(f"ℹ️  {decision['decision']} — {decision.get('reason', '')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
