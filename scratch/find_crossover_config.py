"""
scratch/find_crossover_config.py
=================================
Temporary script to find short_window / long_window combinations that
produce a BUY or SELL signal on real, current AAPL daily bars.

This does NOT modify strategy/config.yaml — it only reports which
combinations would work, so you can pick one to apply manually.

Usage (from project root):
    python scratch/find_crossover_config.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add the project root to sys.path so imports work regardless of how the
# script is invoked (e.g., "python scratch/find_crossover_config.py" from
# the project root).  sys.path is Python's module search path list —
# similar to -I include paths in C++.
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Load credentials the same way orchestrator/loop.py does.
# ---------------------------------------------------------------------------
from data_ingestion.credentials import load_credentials
from data_ingestion import RestClient
from strategy import compute_signal, StrategyConfig

# ---------------------------------------------------------------------------
# Configuration for the search
# ---------------------------------------------------------------------------

# Candidate window sizes to try.
SHORT_CANDIDATES = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20]
LONG_CANDIDATES = [5, 8, 10, 12, 15, 20, 25, 30, 35, 40, 50, 60]

SYMBOL = "AAPL"
TIMEFRAME = "1d"  # Daily bars

# We need at least max(long) + 1 bars for compute_signal. Fetch 80 bars
# to be safe (today minus ~120 calendar days covers ~80 trading days).
LOOKBACK_CALENDAR_DAYS = 120


def main():
    # Load API keys from environment (same path as orchestrator).
    api_key, secret_key = load_credentials()
    print(f"Credentials loaded successfully.")

    # Build date range: from LOOKBACK_CALENDAR_DAYS ago until today.
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    ).strftime("%Y-%m-%d")

    print(f"Fetching {SYMBOL} daily bars from {start_date} to {end_date}...")

    # Fetch bars via data_ingestion.RestClient (read-only market data).
    client = RestClient(api_key, secret_key)
    df = client.get_historical_bars(SYMBOL, TIMEFRAME, start_date, end_date)

    print(f"Received {len(df)} bars. Last bar timestamp: {df.index[-1]}")
    print(f"Last close: {df['close'].iloc[-1]}")
    print()
    print("=" * 72)
    print(f"{'short':<6} {'long':<6} {'signal':<6} {'short_sma':<12} {'long_sma':<12} timestamp")
    print("-" * 72)

    # Track which combinations produce a non-HOLD signal.
    hits = []

    for short_w in SHORT_CANDIDATES:
        for long_w in LONG_CANDIDATES:
            # Only valid if short < long.
            if short_w >= long_w:
                continue

            # Build a StrategyConfig with these window sizes.
            config = StrategyConfig(short_window=short_w, long_window=long_w)

            # compute_signal needs at least long_window + 1 bars.
            if len(df) < long_w + 1:
                print(f"{short_w:<6} {long_w:<6} SKIP (need {long_w + 1} bars, have {len(df)})")
                continue

            result = compute_signal(df, config, symbol=SYMBOL)
            sig = result["signal"]

            if sig != "HOLD":
                short_sma = f"{result['short_sma']:.4f}" if result["short_sma"] else "N/A"
                long_sma = f"{result['long_sma']:.4f}" if result["long_sma"] else "N/A"
                ts = str(result["timestamp"])
                print(f"{short_w:<6} {long_w:<6} {sig:<6} {short_sma:<12} {long_sma:<12} {ts}")
                hits.append({
                    "short_window": short_w,
                    "long_window": long_w,
                    "signal": sig,
                    "short_sma": result["short_sma"],
                    "long_sma": result["long_sma"],
                    "timestamp": str(result["timestamp"]),
                })

    print("=" * 72)

    if not hits:
        print("\nNo BUY or SELL signals found with any combination.")
        print("All tested combinations produce HOLD on current data.")
        print("You may need to widen the search range or try different windows.")
    else:
        print(f"\nFound {len(hits)} combination(s) that produce a signal:")
        for h in hits:
            print(f"  short_window={h['short_window']}, long_window={h['long_window']} "
                  f"-> {h['signal']}  (short_sma={h['short_sma']:.4f}, "
                  f"long_sma={h['long_sma']:.4f})")


if __name__ == "__main__":
    main()
