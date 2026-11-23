# Project Structure

This is an algorithmic trading bot built as five independent modules.
Each module has one job and passes clean, structured data to the next.
Modules must never skip the pipeline order below.

## Pipeline Order (strict)
Data Ingestion → Strategy Engine → Risk Engine → Order Execution
                                       ↑
                          Logging observes all four, always

## Modules

### 1. data_ingestion/
- Fetches historical candles via REST and live prices via WebSocket.
- Outputs clean pandas DataFrames only.
- Has READ-ONLY access to the broker API. Must never place, modify,
  or cancel an order under any circumstance.

### 2. strategy/
- Consumes DataFrames from data_ingestion.
- Computes indicators and produces a signal: BUY, SELL, or HOLD.
- Has NO access to the broker API at all. Pure computation only.

### 3. risk_engine/
- Consumes signals from strategy.
- Approves, resizes, or vetoes every signal before it can proceed.
- Enforces: max position size, max daily loss, max open positions,
  mandatory stop-loss on every trade.
- Has NO ability to place orders itself — approval only, execution
  happens downstream.

### 4. execution/
- The ONLY module allowed to place, modify, or cancel real orders.
- Only accepts input that has already passed through risk_engine.
- Must confirm every order fill and report status back to logging.

### 5. logging/
- Observes every module above and records every signal, every risk
  decision, and every order.
- Runs alongside all four modules, not after them.

## Current Mode
This project is PAPER TRADING ONLY via Alpaca. Do not implement
any code path that could route to a live trading account.