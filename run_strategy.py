"""
run_strategy.py — Standalone script: fetch bars then compute a signal.

Fetches the last 40 daily bars for AAPL using RestClient from data_ingestion,
feeds that DataFrame into compute_signal from the strategy module using the
default config (short_window=10, long_window=30), and prints the full result
dictionary.

Run from the project root with the venv active:
    python run_strategy.py

Requirements:
    - .env file present at the project root with APCA_API_KEY_ID and
      APCA_API_SECRET_KEY set to valid Alpaca paper-trading credentials.
    - venv activated (or run as: venv\\Scripts\\python.exe run_strategy.py)
"""

from datetime import date, timedelta

# load_credentials() reads API keys from .env — raises EnvironmentError if missing.
from data_ingestion.credentials import load_credentials

# RestClient fetches historical bars from Alpaca's market-data (read-only) endpoint.
from data_ingestion.rest_client import RestClient

# compute_signal is the pure-computation function from the strategy module.
from strategy.compute import compute_signal

# load_config reads strategy/config.yaml and returns a validated StrategyConfig.
from strategy.config_loader import load_config


def main() -> None:
    # --- Load Alpaca credentials from .env ---
    api_key, secret_key = load_credentials()

    # --- Build the data ingestion client ---
    client = RestClient(api_key, secret_key)

    # --- Compute a date range that captures ~40 trading days ---
    # 40 trading days ≈ 60 calendar days (accounting for weekends/holidays).
    # We request 60 calendar days to be safe — the API returns only actual
    # market days, and we take the last 40.
    end = date.today()
    start = end - timedelta(days=60)

    start_str = start.isoformat()
    end_str = end.isoformat()

    print(f"Fetching daily bars for AAPL from {start_str} to {end_str} ...")

    # --- Fetch bars ---
    df = client.get_historical_bars(
        symbol="AAPL",
        timeframe="1d",
        start=start_str,
        end=end_str,
    )

    if df.empty:
        print("No bars returned for this date range.")
        return

    # Keep only the last 40 bars (in case the API returned more).
    # .tail(n) returns the last n rows — like slicing from the back of a vector.
    df = df.tail(40)

    print(f"Got {len(df)} bars. Last bar timestamp: {df.index[-1]}")
    print()

    # --- Load the default strategy config (short=10, long=30) ---
    config = load_config("strategy/config.yaml")
    print(f"Strategy config: short_window={config.short_window}, long_window={config.long_window}")
    print()

    # --- Run compute_signal ---
    result = compute_signal(df, config, symbol="AAPL")

    # --- Print the full result dictionary ---
    print("Signal result:")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
