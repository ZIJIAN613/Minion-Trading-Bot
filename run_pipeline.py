# run_pipeline.py
# ================
# Standalone end-to-end smoke script that exercises the first three pipeline
# stages: Data Ingestion → Strategy Engine → Risk Engine.
#
# What it does:
#   1. Fetches the last 40 daily bars for AAPL via RestClient (data_ingestion)
#   2. Feeds those bars into compute_signal (strategy) to get a signal dict
#   3. Feeds the signal dict into evaluate_signal (risk_engine) with a
#      hand-constructed account snapshot
#   4. Prints the final Decision_Dict from the risk engine
#
# Run from the project root with the venv active:
#     python run_pipeline.py
#   or:
#     venv\Scripts\python.exe run_pipeline.py
#
# Requirements:
#   - .env file at the project root with APCA_API_KEY_ID and APCA_API_SECRET_KEY
#   - venv activated with all dependencies installed

from datetime import date, timedelta

# --- Data Ingestion imports ---
from data_ingestion.credentials import load_credentials
from data_ingestion.rest_client import RestClient

# --- Strategy Engine imports ---
from strategy.config_loader import load_config as load_strategy_config
from strategy.compute import compute_signal

# --- Risk Engine imports ---
from risk_engine.config_loader import load_config as load_risk_config
from risk_engine.evaluate import evaluate_signal


def main() -> None:
    # ==================================================================
    # STAGE 1: Data Ingestion — fetch 40 daily bars for AAPL
    # ==================================================================
    print("=" * 60)
    print("STAGE 1: Data Ingestion")
    print("=" * 60)

    # Load Alpaca API credentials from .env file.
    api_key, secret_key = load_credentials()

    # Create the REST client (market-data endpoint only, read-only).
    client = RestClient(api_key, secret_key)

    # Request enough calendar days to cover ~40 trading days.
    # Markets are open roughly 5/7 of calendar days, so 60 calendar days
    # should comfortably yield 40+ trading bars.
    end = date.today()
    start = end - timedelta(days=60)

    print(f"Fetching daily bars for AAPL from {start.isoformat()} to {end.isoformat()} ...")

    df = client.get_historical_bars(
        symbol="AAPL",
        timeframe="1d",
        start=start.isoformat(),
        end=end.isoformat(),
    )

    if df.empty:
        print("ERROR: No bars returned. Check your API credentials and date range.")
        return

    # Take the last 40 bars (in case more were returned).
    # .tail(n) returns the last n rows — like slicing from the back in C++.
    df = df.tail(40)
    print(f"Got {len(df)} daily bars. Last bar: {df.index[-1]}")
    print(f"Last close price: ${df['close'].iloc[-1]:.2f}")
    print()

    # ==================================================================
    # STAGE 2: Strategy Engine — compute signal from bars
    # ==================================================================
    print("=" * 60)
    print("STAGE 2: Strategy Engine")
    print("=" * 60)

    # Load the strategy config (short_window=10, long_window=30).
    strategy_config = load_strategy_config("strategy/config.yaml")
    print(f"Strategy config: short_window={strategy_config.short_window}, "
          f"long_window={strategy_config.long_window}")

    # compute_signal returns a Signal_Dict with keys:
    #   signal, short_sma, long_sma, timestamp, and optionally symbol
    signal_dict = compute_signal(df, strategy_config, symbol="AAPL")

    print(f"Signal: {signal_dict['signal']}")
    print(f"Short SMA: {signal_dict['short_sma']}")
    print(f"Long SMA:  {signal_dict['long_sma']}")
    print(f"Timestamp: {signal_dict['timestamp']}")
    print()

    # ==================================================================
    # STAGE 3: Risk Engine — evaluate signal against guardrails
    # ==================================================================
    print("=" * 60)
    print("STAGE 3: Risk Engine")
    print("=" * 60)

    # Load the risk config from risk_engine/config.yaml.
    risk_config = load_risk_config("risk_engine/config.yaml")
    print(f"Risk config: max_position_percent={risk_config.max_position_percent}%, "
          f"max_daily_loss=${risk_config.max_daily_loss}, "
          f"max_open_positions={risk_config.max_open_positions}, "
          f"stop_loss_percent={risk_config.stop_loss_percent}%")

    # Use the last close price as the current market price.
    # In a real system this would come from a live quote, but for this
    # demo we use the most recent bar's close.
    current_price = float(df["close"].iloc[-1])

    # Hand-constructed account snapshot:
    # - equity: $10,000 (total account value)
    # - cash: $10,000 (all cash available, no positions consuming capital)
    # - open_positions: empty (no current holdings)
    # - daily_realized_pnl: $0 (no trades today)
    account_snapshot = {
        "equity": 10000.0,
        "cash": 10000.0,
        "open_positions": [],
        "daily_realized_pnl": 0.0,
    }
    print(f"Account: equity=${account_snapshot['equity']}, "
          f"cash=${account_snapshot['cash']}, "
          f"open_positions={len(account_snapshot['open_positions'])}, "
          f"daily_pnl=${account_snapshot['daily_realized_pnl']}")
    print(f"Current price: ${current_price:.2f}")
    print()

    # evaluate_signal returns a Decision_Dict with keys:
    #   decision, signal, position_size, stop_loss, reason, timestamp, symbol
    decision = evaluate_signal(signal_dict, current_price, account_snapshot, risk_config)

    # ==================================================================
    # OUTPUT: Final Decision_Dict
    # ==================================================================
    print("=" * 60)
    print("FINAL DECISION")
    print("=" * 60)
    # Print each key-value pair on its own line for readability.
    for key, value in decision.items():
        print(f"  {key}: {value}")
    print()


if __name__ == "__main__":
    # __name__ == "__main__" is True only when this file is run directly
    # (not when imported as a module). This is the Python equivalent of
    # int main() in C++ — the conventional entry point guard.
    main()
