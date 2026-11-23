# Minions — Algorithmic Trading Bot

A personal, non-commercial algorithmic trading bot built for learning. It runs
against **Alpaca paper trading only** — no real money is at risk. Live trading
is explicitly out of scope.

> ⚠️ **Paper trading only.** There is no code path that routes to a live
> trading account, and none should be added while this notice stands.

## Overview

The bot is organized as a strict, one-directional pipeline. Each module has a
single job and passes clean, structured data to the next. Modules never skip
the pipeline order.

```
Data Ingestion → Strategy Engine → Risk Engine → Order Execution
                                        ↑
                           Logging observes all four, always
```

## Modules

| Directory          | Role                                                                                                           |
| ------------------ | -------------------------------------------------------------------------------------------------------------- |
| `data_ingestion/`  | Fetches historical candles (REST) and live prices (WebSocket). Outputs clean pandas DataFrames. **Read-only** broker access — never places, modifies, or cancels orders. |
| `strategy/`        | Consumes DataFrames, computes indicators, and produces a `BUY` / `SELL` / `HOLD` signal. Pure computation, no broker access. |
| `risk_engine/`     | Approves, resizes, or vetoes every signal. Enforces max position size, max daily loss, max open positions, and a mandatory stop-loss. Cannot place orders itself. |
| `execution/`       | The **only** module allowed to place, modify, or cancel orders. Accepts input that has passed through the risk engine, confirms fills, and reports status to logging. |
| `log_system/`      | Observes every module and records every signal, risk decision, and order. Runs alongside the pipeline, not after it. |
| `orchestrator/`    | Drives the pipeline loop, holds run state (`state.json`), and runs safety checks (including the kill switch). |
| `alerting/`        | Sends notifications (e.g. Telegram) for key events such as submitted orders.                                   |
| `tools/dashboard/` | Lightweight local dashboard for monitoring.                                                                    |
| `tests/`           | Unit, property-based (Hypothesis), and integration tests.                                                      |

## Requirements

- Python 3.13
- Dependencies listed in `requirements.txt`:
  - `pandas`, `python-dotenv`, `alpaca-py`, `pyyaml`
  - `pytest`, `hypothesis`, `pytest-asyncio` (testing)

## Setup

```powershell
# Create and activate a virtual environment
python -m venv venv
venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

Create a `.env` file in the project root with your Alpaca **paper** API
credentials:

```
APCA_API_KEY_ID=your_paper_key
APCA_API_SECRET_KEY=your_paper_secret
```

## Running

Each module is configured via its own `config.yaml`.

```powershell
# End-to-end smoke run: Data Ingestion → Strategy → Risk Engine
python run_pipeline.py

# Strategy-only run
python run_strategy.py
```

`run_pipeline.py` fetches recent daily bars for a symbol, computes a signal, and
evaluates it through the risk engine, printing the final decision.

## Testing

```powershell
pytest
```

The suite covers unit behavior, property-based invariants (via Hypothesis),
module purity (strategy and risk engine do no I/O), the read-only guarantee for
data ingestion, execution safety, and kill-switch handling.

## Design constraints

- **Pipeline order is strict** — modules never skip a stage.
- **Separation of concerns** — only `execution/` can touch orders; `strategy/`
  and `risk_engine/` are pure; `data_ingestion/` is read-only.
- **Logging is always on** — it observes every module, not a final step.
- **Paper trading only** — Alpaca paper account, no live routing.
