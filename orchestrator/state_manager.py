# orchestrator/state_manager.py
#
# All logic for reading, writing, and updating the State_File
# (Position_Records, Last_Acted_Bar, pending_decision).
#
# The State_File is the Orchestrator's only durable local state — account
# and position data is always fresh-queried from Alpaca each cycle.
#
# Key design principles:
#   - Atomic writes (write-to-temp-then-rename) to prevent corruption.
#   - Immutable update pattern — functions return NEW StateData rather than
#     mutating the input (makes reasoning about state changes explicit).
#   - Raises ValueError on corrupt JSON (never silently ignores).
#
# Requirements: 1.2, 1.5, 1.6, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd  # pd.Timestamp — used for timestamp reconversion in load_state


# ---------------------------------------------------------------------------
# PositionRecord dataclass
# ---------------------------------------------------------------------------
#
# Represents a single position opened by the Orchestrator.
# This is persisted to the State_File as a JSON object.

@dataclass
class PositionRecord:
    """
    A durable record of a position opened by the Orchestrator.

    Fields:
        symbol            — Ticker (e.g., "AAPL").
        entry_order_id    — Alpaca order ID string.
        stop_loss_order_id — Alpaca stop-loss order ID, or None if not set.
        quantity          — Number of shares held (≥ 1).
        entry_price       — Fill price, rounded to 2 decimal places.
        entry_timestamp   — UTC ISO 8601 string (e.g., "2024-06-15T14:30:00Z").
        status            — One of: "open", "closed", "partially_closed".
        exit_timestamp    — UTC ISO 8601 string when position was closed, or None.
    """
    symbol: str
    entry_order_id: str
    stop_loss_order_id: Optional[str]
    quantity: int
    entry_price: float
    entry_timestamp: str
    status: str  # "open" | "closed" | "partially_closed"
    exit_timestamp: Optional[str] = None


# ---------------------------------------------------------------------------
# StateData dataclass
# ---------------------------------------------------------------------------

@dataclass
class StateData:
    """
    The complete content of the State_File.

    Fields:
        positions        — List of PositionRecord dicts (serialized form).
        last_acted_bar   — UTC ISO 8601 string of the last bar that triggered
                           a cycle, or None if never run.
        pending_decision — Serialized Decision_Dict awaiting deferred execution,
                           or None when no decision is deferred.
        pending_decision_bar_timestamp — The bar_timestamp that triggered the
                           deferred decision.  Used to advance last_acted_bar
                           when the deferred decision resolves.  None when no
                           decision is deferred.
    """
    positions: list = field(default_factory=list)
    last_acted_bar: Optional[str] = None
    pending_decision: Optional[dict] = None
    pending_decision_bar_timestamp: Optional[str] = None


# ---------------------------------------------------------------------------
# load_state
# ---------------------------------------------------------------------------

def load_state(state_file_path: str) -> StateData:
    """
    Load the State_File from disk.

    If the file does not exist, creates and returns an empty StateData.
    If the file exists but contains invalid JSON, raises ValueError.

    Args:
        state_file_path: Path to the state.json file.

    Returns:
        Populated StateData instance.

    Raises:
        ValueError: If the file contains invalid JSON (with file path and
                    parse error in the message).
    """
    # If the file doesn't exist, this is a first run — return empty state.
    # os.path.exists() is like stat() in C — checks whether the path resolves.
    if not os.path.exists(state_file_path):
        return StateData()

    # Read the file content.  'with open(...)' is a context manager that
    # ensures the file handle is closed when we leave the block — like
    # RAII in C++.
    with open(state_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Parse JSON.  json.loads() deserialises a JSON string into Python
    # objects (dict, list, str, int, float, None).  If the content is
    # malformed JSON, it raises json.JSONDecodeError.
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in state file '{state_file_path}': {exc}"
        ) from exc

    # Build StateData from the parsed dict.
    # dict.get(key, default) safely retrieves a value with a fallback,
    # analogous to std::map::find() + default in C++.
    pending = data.get("pending_decision", None)

    # If a pending_decision was persisted, its "timestamp" field was serialized
    # as an ISO 8601 string by _json_default (since pandas Timestamp isn't
    # JSON-native).  The execution module's validate_decision_dict() requires
    # this field to be a timezone-aware pd.Timestamp, so we reconvert it here
    # on load.  This completes the round-trip:
    #   pd.Timestamp → (save_state) → ISO string → (load_state) → pd.Timestamp
    if pending is not None and "timestamp" in pending:
        ts_value = pending["timestamp"]
        # Only convert if it's a string (might already be correct type in
        # edge cases like in-memory round-trip tests).
        if isinstance(ts_value, str):
            # pd.Timestamp() parses ISO 8601 strings and preserves timezone info.
            # If the string ends with "Z" or "+00:00", the result is UTC-aware.
            # If it's somehow naive, we localize to UTC as a safe default.
            restored_ts = pd.Timestamp(ts_value)
            if restored_ts.tzinfo is None:
                restored_ts = restored_ts.tz_localize("UTC")
            pending["timestamp"] = restored_ts

    return StateData(
        positions=data.get("positions", []),
        last_acted_bar=data.get("last_acted_bar", None),
        pending_decision=pending,
        pending_decision_bar_timestamp=data.get("pending_decision_bar_timestamp", None),
    )


# ---------------------------------------------------------------------------
# _json_default — custom serializer for non-primitive types in state JSON
# ---------------------------------------------------------------------------


def _json_default(obj):
    """
    Custom default handler for json.dumps().

    Converts objects that aren't natively JSON-serializable into strings:
      - pandas Timestamp → ISO 8601 string via .isoformat()
      - datetime objects → ISO 8601 string via .isoformat()
      - Any object with an isoformat() method → call it

    This is passed as the default= argument to json.dumps() in save_state().
    Python's json encoder calls this function whenever it encounters an object
    it doesn't know how to serialize — similar to overriding operator<< in C++
    for custom types.

    Args:
        obj: The object that json.dumps() cannot serialize natively.

    Returns:
        A JSON-serializable representation (string) of the object.

    Raises:
        TypeError: If the object has no isoformat() method (preserves the
                   standard json.dumps error for truly unserializable objects).
    """
    # hasattr(obj, "isoformat") returns True for pandas Timestamp, datetime,
    # date, and any object that follows the isoformat() convention.
    if hasattr(obj, "isoformat"):
        return obj.isoformat()

    # Fall through with the standard TypeError so json.dumps() reports
    # unhandled types clearly (same error message as without default=).
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# save_state
# ---------------------------------------------------------------------------

def save_state(state_data: StateData, state_file_path: str) -> None:
    """
    Atomically write the StateData to disk.

    Algorithm:
      1. Serialize StateData to JSON with indent=2 for readability.
      2. Write to a temporary file in the same directory as state_file_path.
      3. os.replace() the temp file over the target (atomic on most OS).

    Using the same directory for the temp file ensures that os.replace()
    is an atomic rename on the same filesystem — this prevents corruption
    if the process is killed during a write (like fsync + rename in C).

    The default= parameter on json.dumps() handles non-JSON-primitive objects
    (e.g., pandas Timestamp, datetime) that may appear in pending_decision.
    This is the Orchestrator's responsibility per Requirement 11.5 — upstream
    modules (strategy, risk_engine) are not required to produce JSON-safe output.

    Args:
        state_data: The StateData to persist.
        state_file_path: Target file path.

    Raises:
        OSError: If the write or rename fails (disk full, permissions, etc.).
    """
    # Build the JSON-serializable dict from the StateData fields.
    data = {
        "positions": state_data.positions,
        "last_acted_bar": state_data.last_acted_bar,
        "pending_decision": state_data.pending_decision,
        "pending_decision_bar_timestamp": state_data.pending_decision_bar_timestamp,
    }

    # json.dumps() serializes a Python object to a JSON string.
    # indent=2 produces human-readable output with 2-space indentation.
    # ensure_ascii=False allows non-ASCII characters through unchanged.
    # default=_json_default handles objects like pandas Timestamp and datetime
    # that are not natively JSON-serializable — converts them to ISO 8601 strings.
    json_str = json.dumps(data, indent=2, ensure_ascii=False, default=_json_default)

    # Determine the directory of the target file — the temp file must be
    # in the same directory so os.replace() is an atomic rename.
    target_dir = os.path.dirname(os.path.abspath(state_file_path))

    # Ensure the target directory exists (handles first-run case).
    # os.makedirs() is like mkdir -p in the shell — creates all intermediate
    # directories as needed.  exist_ok=True prevents an error if already exists.
    os.makedirs(target_dir, exist_ok=True)

    # Write to a temporary file.
    # tempfile.NamedTemporaryFile creates a file with a random name in the
    # specified directory.  We use delete=False because we want the file to
    # persist after we close it — we'll rename it ourselves.
    # mode="w" opens for text writing; suffix helps identify it if something
    # goes wrong.
    tmp_fd = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target_dir,
        suffix=".tmp",
        delete=False,
    )

    try:
        tmp_fd.write(json_str)
        tmp_fd.write("\n")  # Trailing newline — POSIX convention.
        tmp_fd.close()

        # Atomic rename.  os.replace() overwrites the target if it exists.
        # On Windows this is not truly atomic in all edge cases, but for
        # our paper-trading bot's purposes it's sufficient.
        os.replace(tmp_fd.name, state_file_path)

    except Exception:
        # If anything goes wrong, clean up the temp file before re-raising.
        tmp_fd.close()
        # Attempt to remove the temp file — if this fails too, ignore it.
        try:
            os.unlink(tmp_fd.name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# add_position_record
# ---------------------------------------------------------------------------

def add_position_record(
    state_data: StateData,
    order_result: dict,
    symbol: str,
) -> StateData:
    """
    Append a new open PositionRecord from a filled/partial_fill BUY Order_Result.

    Creates a new StateData with the appended record — does NOT mutate the
    input (immutable update pattern for clear state transitions).

    Args:
        state_data:   Current state.
        order_result: Order_Result dict from the Execution module with at
                      least: entry_order_id, stop_loss_order_id, fill_price,
                      filled_quantity, filled_at.
        symbol:       Ticker symbol (e.g., "AAPL").

    Returns:
        New StateData with the new position appended to positions list.
    """
    # Build the position record as a plain dict for JSON serialisation.
    # round(x, 2) rounds to 2 decimal places — standard for price display.
    new_record = {
        "symbol": symbol,
        "entry_order_id": order_result["entry_order_id"],
        "stop_loss_order_id": order_result.get("stop_loss_order_id"),
        "quantity": order_result["filled_quantity"],
        "entry_price": round(order_result["fill_price"], 2),
        "entry_timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "exit_timestamp": None,
    }

    # Create a new positions list (copy + append) rather than mutating.
    # list(...) creates a shallow copy of the original list.
    new_positions = list(state_data.positions) + [new_record]

    # Return new StateData — all other fields preserved unchanged.
    return StateData(
        positions=new_positions,
        last_acted_bar=state_data.last_acted_bar,
        pending_decision=state_data.pending_decision,
        pending_decision_bar_timestamp=state_data.pending_decision_bar_timestamp,
    )


# ---------------------------------------------------------------------------
# update_position_on_sell
# ---------------------------------------------------------------------------

def update_position_on_sell(
    state_data: StateData,
    order_result: dict,
    symbol: str,
) -> StateData:
    """
    Update an existing open PositionRecord based on a SELL Order_Result.

    Rules:
      - If order_result status is "filled": set position status to "closed",
        record exit_timestamp.
      - If order_result status is "partial_fill": decrement quantity by
        filled_quantity.  If remaining quantity reaches zero, set "closed"
        with exit_timestamp.  Otherwise set "partially_closed".

    Creates a new StateData — does NOT mutate the input.

    Args:
        state_data:   Current state.
        order_result: Order_Result dict with at least: status, filled_quantity.
        symbol:       Ticker symbol to match against existing records.

    Returns:
        New StateData with the matching position updated.
    """
    filled_qty = order_result["filled_quantity"]
    result_status = order_result["status"]
    exit_time = datetime.now(timezone.utc).isoformat()

    # Build a new positions list with the matching record updated.
    new_positions = []
    updated = False

    for pos in state_data.positions:
        # We only update the FIRST matching open position for this symbol.
        if (
            not updated
            and pos.get("symbol") == symbol
            and pos.get("status") == "open"
        ):
            # Create a copy of the position dict to avoid mutating the original.
            # dict(pos) creates a shallow copy — like copy constructor in C++.
            updated_pos = dict(pos)

            if result_status == "filled":
                # Full close — mark as closed with exit timestamp.
                updated_pos["status"] = "closed"
                updated_pos["exit_timestamp"] = exit_time
            elif result_status == "partial_fill":
                # Partial close — decrement quantity.
                remaining = updated_pos["quantity"] - filled_qty
                if remaining <= 0:
                    # Edge case: partial fill consumed all remaining shares.
                    updated_pos["status"] = "closed"
                    updated_pos["exit_timestamp"] = exit_time
                    updated_pos["quantity"] = 0
                else:
                    updated_pos["status"] = "partially_closed"
                    updated_pos["quantity"] = remaining

            new_positions.append(updated_pos)
            updated = True
        else:
            new_positions.append(pos)

    return StateData(
        positions=new_positions,
        last_acted_bar=state_data.last_acted_bar,
        pending_decision=state_data.pending_decision,
        pending_decision_bar_timestamp=state_data.pending_decision_bar_timestamp,
    )


# ---------------------------------------------------------------------------
# format_account_snapshot
# ---------------------------------------------------------------------------

def format_account_snapshot(account, positions: list) -> dict:
    """
    Transform Alpaca TradeAccount and Position objects into the
    Risk Engine's expected account_snapshot format.

    Algorithm:
      1. Extract equity = float(account.equity).
         If equity <= 0, raise ValueError (cycle should be skipped).
      2. Extract cash = float(account.cash).
         If cash < 0, raise ValueError (cycle should be skipped and retried,
         same as the equity <= 0 case in step 1).
      3. Compute daily_realized_pnl = float(account.equity) - float(account.last_equity).
      4. Build open_positions list:
         For each position in positions:
           - symbol = position.symbol
           - size = int(float(position.qty))  # truncate fractional to integer
           - If size <= 0, skip this position
           - Append {"symbol": symbol, "size": size}
      5. Return the formatted dict.

    Args:
        account:   Alpaca TradeAccount object (has .equity, .cash, .last_equity).
        positions: List of Alpaca Position objects (each has .symbol, .qty).

    Returns:
        account_snapshot dict compatible with Risk Engine:
        {"equity": float, "cash": float, "open_positions": [...],
         "daily_realized_pnl": float}

    Raises:
        ValueError: If equity is zero or negative, or if cash is negative.
    """
    # Step 1: Extract equity.
    # float() converts from string or Decimal — Alpaca returns string values
    # for monetary fields in their SDK objects.
    equity = float(account.equity)
    if equity <= 0:
        raise ValueError(
            f"Account equity is {equity} (must be positive). "
            f"Cycle should be skipped."
        )

    # Step 2: Extract cash.
    # Negative cash on a paper account indicates a data error or unexpected
    # Alpaca state — treat the same as non-positive equity (skip and retry).
    cash = float(account.cash)
    if cash < 0:
        raise ValueError(
            f"Account cash is {cash} (must be non-negative). "
            f"Cycle should be skipped."
        )

    # Step 3: Compute daily realized P&L.
    # last_equity is the previous day's closing equity as reported by Alpaca.
    # The difference gives us an approximation of today's realized P&L.
    daily_realized_pnl = float(account.equity) - float(account.last_equity)

    # Step 4: Build open_positions list.
    # int(float(qty)) truncates fractional shares to a whole integer (floor
    # towards zero).  Positions with size <= 0 after truncation are excluded.
    open_positions = []
    for pos in positions:
        # float() first in case qty is a string (Alpaca returns strings).
        # int() then truncates toward zero — like static_cast<int> in C++.
        size = int(float(pos.qty))
        if size <= 0:
            continue
        open_positions.append({
            "symbol": pos.symbol,
            "size": size,
        })

    # Step 5: Return the formatted snapshot.
    return {
        "equity": equity,
        "cash": cash,
        "open_positions": open_positions,
        "daily_realized_pnl": daily_realized_pnl,
    }
