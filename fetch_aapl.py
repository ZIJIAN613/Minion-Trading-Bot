"""
fetch_aapl.py — Standalone smoke script for RestClient.

Fetches the last 5 daily bars for AAPL from Alpaca's IEX feed and prints
the resulting DataFrame to the terminal.

Run from the project root with the venv active:
    python fetch_aapl.py

Requirements:
    - .env file present at the project root with APCA_API_KEY_ID and
      APCA_API_SECRET_KEY set to valid Alpaca paper-trading credentials.
    - venv activated (or run as: venv\Scripts\python.exe fetch_aapl.py)
"""

from datetime import date, timedelta

# load_credentials() reads APCA_API_KEY_ID and APCA_API_SECRET_KEY from .env.
# It raises EnvironmentError if either variable is missing or empty.
from data_ingestion.credentials import load_credentials

# RestClient wraps StockHistoricalDataClient (market-data endpoint only).
# It never touches the trading endpoint — safe to use here.
from data_ingestion.rest_client import RestClient


def main() -> None:
    # --- Load credentials from .env ---
    # Tuple unpacking: api_key, secret_key = (val1, val2)
    # Python's equivalent of std::tie(api_key, secret_key) = load_credentials()
    api_key, secret_key = load_credentials()

    # --- Build the client ---
    client = RestClient(api_key, secret_key)

    # --- Compute a date range that captures at least 5 trading days ---
    # We request the last 10 calendar days so that weekends and holidays
    # don't cause us to miss 5 full trading days. The API returns only
    # actual market days, so we'll get ≤ 10 bars; we then take the last 5.
    end = date.today()
    start = end - timedelta(days=10)

    # date.isoformat() produces "YYYY-MM-DD" — the format RestClient expects.
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

    # --- Take the last 5 rows ---
    # DataFrame.tail(n) returns the last n rows — like slicing a vector from
    # the back in C++: v[v.size()-5 .. v.size()]
    last_five = df.tail(5)

    print(f"\nLast {len(last_five)} daily bar(s) for AAPL:\n")
    print(last_five.to_string())   # to_string() prints all columns without truncation


if __name__ == "__main__":
    # __name__ == "__main__" is True only when this file is run directly
    # (not when it is imported as a module). This is the Python equivalent of
    # int main() in C++ — the conventional entry point guard.
    main()
