# orchestrator/loop.py
#
# Main loop logic for the Orchestrator module: new-bar checking, cycle
# execution, error classification, shutdown handling, and deferred execution.
#
# This file will be built incrementally across multiple tasks:
#   Task 4.1 — _create_paper_client(), _check_new_bar()
#   Task 4.3 — _run_cycle()
#   Task 5.1 — _classify_error()
#   Task 7.1 — run_loop() initialization
#   Task 7.2 — run_loop() main loop, shutdown, deferred execution
#
# Requirements: 2.1, 2.2, 2.3, 2.5, 4.1–4.7, 5.1–5.9, 9.1–9.6, 10.1–10.7,
#               11.1–11.6

import logging
import os
import signal
import sys
import time
import threading
from datetime import datetime, timezone, timedelta

import pandas as pd

# Alpaca SDK — TradingClient is the ONLY trading-capable class used here.
# It is instantiated with paper=True as a HARD-CODED LITERAL — see
# _create_paper_client() below.
from alpaca.trading.client import TradingClient

# Import from sibling modules within the project.
from data_ingestion import RestClient
from data_ingestion.credentials import load_credentials

from strategy import compute_signal
from strategy import load_config as load_strategy_config
from strategy import StrategyConfig

from risk_engine import evaluate_signal
from risk_engine import load_config as load_risk_config

from execution import execute_decision
from execution import load_config as load_execution_config

from orchestrator.config_loader import OrchestratorConfig, load_config
from orchestrator.state_manager import (
    StateData,
    load_state,
    save_state,
    add_position_record,
    update_position_on_sell,
    format_account_snapshot,
)
from orchestrator.safety_check import run_safety_check

# Import the alerting module for fire-and-forget Telegram notifications.
# alerting.send_alert() never raises — safe to call from any code path.
import alerting

# Reuse data_ingestion's get_logger() which attaches a _JsonFormatter.
# This ensures orchestrator logs are structured JSON — the same format the
# tools/dashboard expects for color-coded log display.
from data_ingestion.logger import get_logger

# Module-level logger — consistent with project conventions.
# All orchestrator logs use the name "orchestrator".
# get_logger() attaches a StreamHandler(stderr) with _JsonFormatter if the
# logger doesn't have handlers yet.  Plain logging.getLogger() would NOT
# attach any formatter, producing raw text that the dashboard can still
# display but cannot color-code by level.
logger = get_logger("orchestrator")


# ---------------------------------------------------------------------------
# ContractViolationError — custom exception for stage return-type violations
# ---------------------------------------------------------------------------

class ContractViolationError(RuntimeError):
    """
    Raised when a pipeline stage returns None or an unexpected type.

    This indicates an internal wiring bug, NOT a transient external failure.
    The Orchestrator SHALL halt on this error (classified as "post_order"),
    because retrying cannot fix a code-level contract violation.

    This is a subclass of RuntimeError (not ValueError or TypeError) to
    distinguish it from validation errors raised by the pipeline modules
    themselves.
    """
    pass


# ---------------------------------------------------------------------------
# _create_paper_client
# ---------------------------------------------------------------------------

def _create_paper_client(api_key: str, secret_key: str) -> TradingClient:
    """
    Instantiate Alpaca TradingClient with hard-coded paper=True.

    This function:
      1. Creates TradingClient(api_key, secret_key, paper=True)
         ^^^ paper=True is a HARD-CODED LITERAL — NEVER change this ^^^
      2. Verifies the base URL contains "paper" (case-insensitive)
         If not → raises RuntimeError (live trading FORBIDDEN)

    Unlike the execution module's _create_paper_client, this function receives
    credentials as arguments (the caller loads them) rather than loading them
    internally.  This keeps credential loading as a single explicit step in
    the initialization sequence.

    Args:
        api_key:    Alpaca API key ID (from load_credentials()).
        secret_key: Alpaca API secret key (from load_credentials()).

    Returns:
        A TradingClient instance connected to the paper-trading endpoint.

    Raises:
        RuntimeError: If the client's base URL does not contain "paper".
    """
    # Instantiate the Alpaca paper-trading client.
    # paper=True is a LITERAL — never a variable, never from config.
    # This is the ONLY module (besides execution/) that uses TradingClient.
    client = TradingClient(api_key, secret_key, paper=True)  # NEVER change this — paper trading ONLY

    # Defense-in-depth: verify the base URL contains "paper" (case-insensitive).
    # Even if paper=True was somehow ineffective, this catches it before any
    # account query or trading operation can occur.
    #
    # The Alpaca SDK stores the base URL in different attributes depending on
    # the version.  We check both common locations.
    # getattr(obj, name, default) is Python's equivalent of a C++ ternary:
    #   auto val = obj.has_attr(name) ? obj.name : default;
    base_url = str(
        getattr(client, "_base_url", "")
        or getattr(client, "base_url", "")
        or ""
    )

    # str.lower() converts to lowercase — like std::tolower in C++.
    # 'in' checks substring membership — like std::string::find() != npos.
    if "paper" not in base_url.lower():
        raise RuntimeError(
            "Paper-trading safety violation: The Alpaca client base URL "
            f"does not contain 'paper': '{base_url}'. "
            "The orchestrator module is restricted to paper trading ONLY."
        )

    return client


# ---------------------------------------------------------------------------
# _check_new_bar
# ---------------------------------------------------------------------------

def _check_new_bar(
    rest_client: RestClient,
    symbol: str,
    last_acted_bar: str | None,
) -> tuple[bool, str | None]:
    """
    Check if a new daily bar is available for the given symbol.

    Fetches the latest daily bar via the Data Ingestion RestClient,
    then compares its date portion (YYYY-MM-DD UTC) against the
    last_acted_bar timestamp stored in the State_File.

    Algorithm:
      1. Compute date range: end = today (UTC), start = end - 5 days
         (enough to guarantee at least one trading day is included,
          even across weekends/holidays).
      2. Fetch daily bars via rest_client.get_historical_bars().
      3. If the result is empty → return (False, None) — no bar available.
      4. Extract the date portion of the latest bar's timestamp.
      5. If last_acted_bar is None → return (True, latest_bar_date).
         (First run: treat any available bar as new.)
      6. Compare dates: if latest > last_acted → (True, latest_bar_date).
         Otherwise → (False, latest_bar_date).

    Args:
        rest_client:    Data Ingestion RestClient instance.
        symbol:         Ticker to check (e.g., "AAPL").
        last_acted_bar: UTC ISO 8601 string of the last bar that triggered
                        a cycle, or None if never run.

    Returns:
        (is_new, bar_timestamp) where:
        - is_new = True if the latest bar is newer than last_acted_bar
                   (or if last_acted_bar is None — first run).
        - bar_timestamp = ISO 8601 date string of the latest bar
                          (e.g., "2024-06-15"), or None if no bars available.

    Raises:
        Any exception from rest_client.get_historical_bars() propagates
        to the caller (the caller logs and retries next interval).
    """
    # Step 1: Compute the date range for fetching.
    # We look back 5 calendar days to ensure we capture at least one
    # trading day (accounts for weekends and up to 2 consecutive holidays).
    # datetime.now(timezone.utc) gives the current UTC time — like
    # std::chrono::system_clock::now() in C++.
    now_utc = datetime.now(timezone.utc)
    end_date = now_utc.strftime("%Y-%m-%d")
    # timedelta is Python's equivalent of std::chrono::duration — it
    # represents a time difference that can be added/subtracted from dates.
    start_date = (now_utc - timedelta(days=5)).strftime("%Y-%m-%d")

    # Step 2: Fetch daily bars.
    # This may raise IOError or ValueError — we let it propagate.
    df = rest_client.get_historical_bars(symbol, "1d", start_date, end_date)

    # Step 3: Handle empty result.
    # df.empty is True when the DataFrame has zero rows.
    if df.empty:
        return (False, None)

    # Step 4: Extract the date portion of the latest bar's timestamp.
    # df.index is a UTC-aware DatetimeIndex sorted ascending.
    # df.index[-1] is the last (most recent) timestamp.
    # .strftime("%Y-%m-%d") formats it as a date string like "2024-06-15".
    latest_bar_timestamp = df.index[-1]
    latest_bar_date = latest_bar_timestamp.strftime("%Y-%m-%d")

    # Step 5: First run (no previous bar) → treat as new.
    if last_acted_bar is None:
        return (True, latest_bar_date)

    # Step 6: Compare date portions.
    # Extract the date portion of last_acted_bar.
    # last_acted_bar may be a full ISO 8601 datetime like "2024-06-14T00:00:00Z"
    # or just a date like "2024-06-14".  We take the first 10 characters
    # (YYYY-MM-DD) for comparison.
    last_acted_date = last_acted_bar[:10]

    # String comparison of YYYY-MM-DD format works correctly for date ordering
    # because the format is lexicographically sortable (year > month > day).
    is_new = latest_bar_date > last_acted_date

    return (is_new, latest_bar_date)



# ---------------------------------------------------------------------------
# _run_cycle
# ---------------------------------------------------------------------------

def _run_cycle(
    rest_client: RestClient,
    trading_client: TradingClient,
    strategy_config: StrategyConfig,
    risk_config,
    execution_config,
    orch_config: OrchestratorConfig,
    state_data: StateData,
    cycle_number: int,
) -> tuple[str, dict | None]:
    """
    Execute one complete pipeline cycle.

    Runs the four pipeline stages in strict order:
      1. Query account state from Alpaca (fresh every cycle).
      2. Data Ingestion — fetch historical bars.
      3. Strategy — compute signal from bars.
      4. Risk Engine — evaluate signal against account state.
      5. Check market open (via Alpaca clock endpoint).
      6. Execution — place orders if approved and market is open.

    After each stage, the return value is validated against the expected
    type.  If a stage returns None or an unexpected type, a
    ContractViolationError is raised (this will be classified as "post_order"
    by _classify_error, halting the loop — not retried).

    Args:
        rest_client:       Data Ingestion RestClient instance.
        trading_client:    Alpaca TradingClient (paper) for account queries.
        strategy_config:   Validated StrategyConfig (has .long_window).
        risk_config:       Validated RiskConfig.
        execution_config:  Validated ExecutionConfig.
        orch_config:       Validated OrchestratorConfig (has .symbol).
        state_data:        Current StateData (for logging/context).
        cycle_number:      Current cycle count (for logging).

    Returns:
        (outcome, order_result) where:
        - outcome is one of: "filled", "partial_fill", "vetoed", "no_action",
          "skipped", "cancelled", "expired", "rejected", "timeout", "deferred"
        - order_result is the Order_Result dict from Execution, or None if
          the cycle ended before reaching Execution (vetoed/no_action/deferred).

    Raises:
        ContractViolationError: If any stage returns None or unexpected type.
        Any exception from the pipeline stages (ValueError, TypeError, IOError,
        etc.) propagates to the caller for classification.
    """
    symbol = orch_config.symbol

    # ==================================================================
    # Stage 1: Query account state from Alpaca
    # ==================================================================
    # Fresh-query every cycle — never cached (Requirement 1.3).
    # trading_client.get_account() returns a TradeAccount object.
    # trading_client.get_all_positions() returns a list of Position objects.
    account = trading_client.get_account()
    positions = trading_client.get_all_positions()

    # Validate: account must not be None (contract with Alpaca SDK).
    if account is None:
        raise ContractViolationError(
            "Stage 'account_query': trading_client.get_account() returned None. "
            "Expected a TradeAccount object."
        )

    # Format the account data into the Risk Engine's expected schema.
    # This raises ValueError if equity <= 0 or cash < 0 (caller skips cycle).
    account_snapshot = format_account_snapshot(account, positions)

    # ==================================================================
    # Stage 2: Data Ingestion — fetch historical bars
    # ==================================================================
    # Compute the start date: at least long_window + 1 trading days back.
    # We use calendar days with a generous margin: long_window * 2 gives
    # enough room for weekends and holidays.
    now_utc = datetime.now(timezone.utc)
    days_back = (strategy_config.long_window + 1) * 2  # generous margin
    start_date = (now_utc - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = now_utc.strftime("%Y-%m-%d")

    # Fetch daily bars.  This may raise IOError (network) or ValueError
    # (invalid args) — both propagate to the caller for classification
    # as pre-order failures.
    bars_df = rest_client.get_historical_bars(symbol, "1d", start_date, end_date)

    # Contract check: must return a pandas DataFrame (not None).
    if not isinstance(bars_df, pd.DataFrame):
        raise ContractViolationError(
            f"Stage 'data_ingestion': get_historical_bars() returned "
            f"{type(bars_df).__name__}, expected pandas DataFrame."
        )

    # ==================================================================
    # Stage 3: Strategy — compute signal from bars
    # ==================================================================
    # compute_signal(df, config, symbol=symbol) returns a Signal_Dict.
    # If insufficient data, it returns {"signal": "HOLD", ...} — this is
    # NOT a failure, it's a valid response.
    signal_dict = compute_signal(bars_df, strategy_config, symbol=symbol)

    # Contract check: must return a dict (not None).
    if not isinstance(signal_dict, dict):
        raise ContractViolationError(
            f"Stage 'strategy': compute_signal() returned "
            f"{type(signal_dict).__name__}, expected dict."
        )

    # ==================================================================
    # Stage 4: Risk Engine — evaluate signal against account state
    # ==================================================================
    # Extract current_price from the close price of the most recent bar.
    # df["close"].iloc[-1] gives the last row's close value.
    # This is the price used for position sizing and stop-loss computation.
    current_price = float(bars_df["close"].iloc[-1])

    # evaluate_signal(signal_dict, current_price, account_snapshot, config)
    # returns a Decision_Dict.  It may raise TypeError or ValueError for
    # invalid inputs — those propagate as pre-order failures.
    decision_dict = evaluate_signal(
        signal_dict, current_price, account_snapshot, risk_config
    )

    # Contract check: must return a dict (not None).
    if not isinstance(decision_dict, dict):
        raise ContractViolationError(
            f"Stage 'risk_engine': evaluate_signal() returned "
            f"{type(decision_dict).__name__}, expected dict."
        )

    # ==================================================================
    # Check for Correct Declines (short-circuit before Execution)
    # ==================================================================
    # If the Risk Engine vetoed or returned NO_ACTION, the cycle is complete.
    # These are correct decisions — NOT failures (Requirement 5.7).
    decision = decision_dict.get("decision", "")

    if decision == "VETOED":
        return ("vetoed", None)

    if decision == "NO_ACTION":
        return ("no_action", None)

    # ==================================================================
    # Stage 5: Check market open (Alpaca clock endpoint)
    # ==================================================================
    # Only proceed with Execution if the market is currently open.
    # trading_client.get_clock() returns a Clock object with .is_open (bool).
    clock = trading_client.get_clock()

    if clock is None:
        raise ContractViolationError(
            "Stage 'market_check': trading_client.get_clock() returned None. "
            "Expected a Clock object."
        )

    # If market is closed, defer execution.
    # The caller is responsible for persisting the Decision_Dict to the
    # state file and waiting for market open (handled in run_loop).
    if not clock.is_open:
        return ("deferred", decision_dict)

    # ==================================================================
    # Stage 6: Execution — place orders if approved
    # ==================================================================
    # execute_decision(decision_dict, config) returns an Order_Result dict.
    # It catches all API errors internally and returns them as status="error"
    # in the Order_Result — it never raises after validation passes.
    # However, it CAN raise TypeError/ValueError during validation if the
    # Decision_Dict is somehow malformed (which would be a bug in our wiring).
    order_result = execute_decision(decision_dict, execution_config)

    # Contract check: must return a dict (not None).
    if not isinstance(order_result, dict):
        raise ContractViolationError(
            f"Stage 'execution': execute_decision() returned "
            f"{type(order_result).__name__}, expected dict."
        )

    # ==================================================================
    # Map Order_Result status to cycle outcome
    # ==================================================================
    status = order_result.get("status", "")

    # Direct mapping of Execution module's terminal statuses to cycle outcomes.
    # These are all valid, non-error outcomes.
    valid_outcomes = {
        "filled", "partial_fill", "skipped", "cancelled",
        "expired", "rejected", "timeout",
    }

    if status in valid_outcomes:
        return (status, order_result)

    # status="error" — this will be classified by _classify_error() in the
    # caller based on whether entry_order_id is present.
    if status == "error":
        return ("error", order_result)

    # Unknown status — treat as contract violation.
    raise ContractViolationError(
        f"Stage 'execution': execute_decision() returned unknown status "
        f"'{status}'. Expected one of: {valid_outcomes | {'error'}}."
    )



# ---------------------------------------------------------------------------
# _classify_error
# ---------------------------------------------------------------------------

def _classify_error(
    stage: str,
    error: Exception,
    order_result: dict | None,
) -> str:
    """
    Classify an error as "pre_order" (safe to retry) or "post_order" (halt).

    This function determines whether a failure that occurred during a pipeline
    cycle is safe to skip-and-retry on the next cycle, or whether it represents
    an ambiguous/dangerous state that requires halting the loop for manual
    intervention.

    Rules:
      - Errors in account_query, data_ingestion, strategy, risk_engine →
        "pre_order" (no order was at risk, safe to self-heal on retry).

      - Execution returns Order_Result with status="error" and
        entry_order_id=None → "pre_order" (error occurred before any order
        was submitted to Alpaca).

      - Execution returns Order_Result with status="error" and
        entry_order_id!=None → "post_order" (an order was submitted but its
        state is ambiguous — manual inspection required).

      - Raw exception from Execution (no Order_Result returned) →
        "post_order" (cannot determine whether an order was submitted before
        the exception occurred).

      - A pipeline stage returning an unexpected type or None where a valid
        output is required (ContractViolationError, per Requirement 3.2) →
        "post_order" (this indicates an internal contract violation, not a
        transient external failure — retrying will not fix it).

    Args:
        stage:        The pipeline stage where the error occurred.  One of:
                      "account_query", "data_ingestion", "strategy",
                      "risk_engine", "execution".
        error:        The exception that was caught.
        order_result: The Order_Result dict if one was returned by Execution
                      before the error was identified, or None if the error
                      was a raw exception (no structured result).

    Returns:
        "pre_order" — safe to skip this cycle and continue to next.
        "post_order" — must halt the loop immediately.
    """
    # ------------------------------------------------------------------
    # Rule 1: ContractViolationError always halts (internal bug).
    # ------------------------------------------------------------------
    # ContractViolationError means a stage returned None or an unexpected
    # type.  This is a code-level defect, not a transient API issue — retrying
    # the same code path will produce the same failure.
    if isinstance(error, ContractViolationError):
        return "post_order"

    # ------------------------------------------------------------------
    # Rule 2: Pre-execution stages are always safe to retry.
    # ------------------------------------------------------------------
    # Errors in account_query, data_ingestion, strategy, or risk_engine
    # occur BEFORE any order is submitted.  No money is at risk — the
    # next cycle will query fresh data and try again.
    pre_execution_stages = {"account_query", "data_ingestion", "strategy", "risk_engine"}
    if stage in pre_execution_stages:
        return "pre_order"

    # ------------------------------------------------------------------
    # Rule 3: Execution stage — classify based on entry_order_id.
    # ------------------------------------------------------------------
    if stage == "execution":
        # If we have an Order_Result, check whether an order was submitted.
        if order_result is not None and isinstance(order_result, dict):
            entry_order_id = order_result.get("entry_order_id")

            if entry_order_id is None:
                # Error occurred BEFORE order submission — safe to retry.
                return "pre_order"
            else:
                # Error occurred AFTER order submission — state is ambiguous.
                # The order may be partially filled, pending, or in an unknown
                # state.  Halt for manual inspection.
                return "post_order"
        else:
            # Raw exception from Execution (no Order_Result returned).
            # We cannot determine whether an order was submitted before the
            # exception occurred.  Conservative choice: halt.
            return "post_order"

    # ------------------------------------------------------------------
    # Fallback: unknown stage — conservative choice is to halt.
    # ------------------------------------------------------------------
    # This should never happen in practice (all stages are enumerated above),
    # but if a new stage is added without updating this function, halting
    # is the safe default.
    return "post_order"



# ---------------------------------------------------------------------------
# _check_kill_switch — Kill Switch File Existence Check
# ---------------------------------------------------------------------------

def _check_kill_switch(file_path: str) -> bool:
    """
    Check whether the kill switch file exists on disk.

    This function is called at the top of every loop iteration and during
    interruptible sleeps.  It uses os.path.exists() — the simplest cross-
    platform mechanism for file existence checks.

    The function only checks file EXISTENCE — the file's content, size,
    ownership, and permissions are irrelevant (Requirement 1.4).

    If os.path.exists() itself raises an OS-level exception (e.g.,
    PermissionError on the parent directory), the exception is caught and
    logged at WARNING level.  A failed check does NOT halt the loop —
    we return False and let the loop continue normally (Requirement 1.5).

    Args:
        file_path: The configured kill switch file path (absolute or relative).

    Returns:
        True  — the kill switch file exists (loop should halt).
        False — the file does not exist OR the check failed.
    """
    try:
        # os.path.exists(path) returns True if path refers to an existing
        # file or directory.  Returns False if the path doesn't exist.
        # On some platforms, it may raise OSError if the process lacks
        # permission to stat the parent directory.
        return os.path.exists(file_path)

    except OSError as exc:
        # OS-level exception during the existence check — log WARNING and
        # return False.  This is a defensive catch: in practice, os.path.exists()
        # usually returns False on permission errors, but some OS/filesystem
        # combinations can raise instead.
        #
        # We log as structured JSON (matching project convention) with the
        # event name, exception type, and truncated message.
        logger.warning(
            "Kill switch check failed due to OS error — continuing loop.",
            extra={
                "event": "kill_switch_check_failed",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:200],
            },
        )
        return False


# ---------------------------------------------------------------------------
# _interruptible_wait — Sleep With Kill Switch + Shutdown Checks
# ---------------------------------------------------------------------------

def _interruptible_wait(
    shutdown_event: threading.Event,
    timeout: float,
    kill_switch_file_path: str,
    check_interval: float = 5.0,
) -> str:
    """
    Sleep for up to `timeout` seconds, but wake early if:
      - shutdown_event is set (graceful shutdown via SIGINT/SIGTERM), OR
      - the kill switch file appears on disk.

    Instead of one long sleep, this function splits the wait into shorter
    chunks of `check_interval` seconds.  Between each chunk it checks both
    wake conditions.  This gives the kill switch a worst-case response
    latency of `check_interval` seconds (default 5s) rather than the full
    polling interval (which could be up to 60 minutes).

    The function uses shutdown_event.wait(timeout) for each chunk — this is
    the same pattern already used elsewhere in the Orchestrator for
    interruptible sleeping.  shutdown_event.wait(timeout) both sleeps AND
    checks whether the event has been set, in a single atomic operation
    provided by Python's threading module.  It returns True if the event
    was set, or False if the timeout expired — like a C++
    condition_variable::wait_for() returning cv_status::timeout or no_timeout.

    Args:
        shutdown_event:         threading.Event for graceful shutdown
                                (set by the SIGINT/SIGTERM signal handler).
        timeout:                Total sleep time in seconds.
        kill_switch_file_path:  Path to check for the kill switch file.
        check_interval:         How often (seconds) to wake and check for
                                kill switch.  Default 5s provides responsive
                                detection without excessive disk polling.

    Returns:
        "timeout"     — the full wait elapsed without interruption.
        "shutdown"    — shutdown_event was set during the wait.
        "kill_switch" — the kill switch file was detected during the wait.
    """
    # Track how much time remains.  We use time.monotonic() for elapsed-time
    # measurements because it is not affected by system clock adjustments —
    # like std::chrono::steady_clock in C++.
    elapsed = 0.0
    start = time.monotonic()

    while elapsed < timeout:
        # Calculate how long to sleep in this chunk.  The final chunk may be
        # shorter than check_interval if the remaining time is less.
        # min(a, b) returns the smaller of two values — same as std::min.
        remaining = timeout - elapsed
        chunk = min(check_interval, remaining)

        # shutdown_event.wait(timeout=chunk) blocks for up to `chunk` seconds.
        # Returns True immediately if the event is already set or becomes set
        # during the wait, False if the timeout expires first.
        was_set = shutdown_event.wait(timeout=chunk)

        if was_set:
            # The shutdown signal handler called shutdown_event.set() — time
            # to exit gracefully.  We clear the event so subsequent waits in
            # the same loop iteration don't see a stale signal.
            # NOTE: We do NOT clear here — the caller (run_loop) decides
            # whether to clear based on what it does next.
            return "shutdown"

        # Also check is_set() as a fallback — in case the signal handler
        # set the event between our wait() returning and this check.
        if shutdown_event.is_set():
            return "shutdown"

        # Check for kill switch file between sleep chunks.
        if _check_kill_switch(kill_switch_file_path):
            return "kill_switch"

        # Update elapsed time for the next iteration.
        elapsed = time.monotonic() - start

    # Full timeout expired without either condition firing.
    return "timeout"


# ---------------------------------------------------------------------------
# _kill_switch_shutdown — Kill Switch Shutdown Sequence
# ---------------------------------------------------------------------------

def _kill_switch_shutdown(orch_config, state_data, cycle_number):
    """
    Execute the kill switch shutdown sequence.

    This function is called when _check_kill_switch() returns True at the
    top of the main loop.  It performs the following steps in order:
      (a) Log a CRITICAL structured JSON event.
      (b) Dispatch a kill_switch_triggered Telegram alert.
      (c) Flush the state file to disk.
      (d) Exit the process with code 1 (clean kill switch activation).

    If any step fails (most likely the state flush raising OSError), the
    function logs a WARNING with the failure details and exits with code 2
    (dirty kill switch shutdown — distinguishable from clean exit code 1).

    Exit codes:
      1 — All shutdown steps succeeded (clean kill switch activation).
      2 — One or more shutdown steps failed (dirty shutdown).

    Args:
        orch_config:  The OrchestratorConfig instance (has kill_switch_file_path,
                      state_file_path).
        state_data:   The current StateData to flush to disk.
        cycle_number: The current cycle count (included in the Telegram alert).

    Returns:
        Never returns — always calls sys.exit().
    """
    try:
        # Step (a): Log CRITICAL structured event.
        # Matches the project's structured JSON logging convention — the
        # "extra" dict is serialized into the JSON log line by _JsonFormatter.
        logger.critical(
            "Kill switch triggered.",
            extra={
                "event": "kill_switch_triggered",
                "file_path": orch_config.kill_switch_file_path,
            },
        )

        # Step (b): Dispatch Telegram alert.
        # alerting.send_alert() never raises (fire-and-forget), but we keep
        # it inside the try block for defense-in-depth.
        alerting.send_alert("kill_switch_triggered", {"cycle_number": cycle_number})

        # Step (c): Flush state file to disk.
        # save_state() uses atomic write-to-temp-then-rename.
        # This CAN raise OSError (disk full, permissions, etc.).
        save_state(state_data, orch_config.state_file_path)

        # Step (d): Stop the Telegram bot listener thread (if running).
        alerting.stop_bot()

        # Step (e): All steps succeeded → clean kill switch exit.
        sys.exit(1)

    except SystemExit:
        # sys.exit(1) raises SystemExit (a BaseException subclass, NOT
        # Exception).  We must re-raise it so the process actually exits.
        # Without this, the "except Exception" below would NOT catch it
        # (SystemExit is not a subclass of Exception), but being explicit
        # here makes the control flow crystal clear for readers.
        raise

    except Exception as e:
        # Any step failed (most likely save_state raising OSError).
        # Log the failure with structured JSON so it's diagnosable after
        # the fact, then exit with code 2 (dirty shutdown).
        logger.warning(
            "Kill switch shutdown sequence failed.",
            extra={
                "event": "kill_switch_shutdown_failed",
                "error_type": type(e).__name__,
                "error_message": str(e)[:200],
            },
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# _post_order_halt_shutdown — Post-Order Failure Shutdown Sequence
# ---------------------------------------------------------------------------

def _post_order_halt_shutdown(orch_config, state_data, cycle_number, error_context):
    """
    Execute the post-order failure shutdown sequence.

    This function is called when a post-order failure halts the loop (e.g.,
    an order was submitted but its final state is ambiguous).  It performs:
      (a) Dispatch a halt_the_loop_error Telegram alert with error details.
      (b) Flush the state file to disk.
      (c) Exit the process with code 3 (clean post-order halt).

    If any step fails, logs a WARNING and exits with code 4 (dirty halt).

    Exit codes:
      3 — All shutdown steps succeeded (clean post-order halt).
      4 — One or more shutdown steps failed (dirty halt).

    Args:
        orch_config:   The OrchestratorConfig instance (has state_file_path).
        state_data:    The current StateData to flush to disk.
        cycle_number:  The current cycle count (for context).
        error_context: Dict with keys like "error_details", "symbol",
                       "entry_order_id" describing the post-order failure.

    Returns:
        Never returns — always calls sys.exit().
    """
    try:
        # Step (a): Dispatch Telegram alert with error context.
        # Truncate error_details to 200 chars to avoid flooding Telegram.
        alerting.send_alert("halt_the_loop_error", {
            "error_summary": str(error_context.get("error_details", ""))[:200],
            "symbol": error_context.get("symbol", ""),
            "order_id": error_context.get("entry_order_id", ""),
        })

        # Step (b): Flush state file to disk.
        save_state(state_data, orch_config.state_file_path)

        # Step (c): Stop the Telegram bot listener thread (if running).
        alerting.stop_bot()

        # Step (d): All steps succeeded → clean post-order halt.
        sys.exit(3)

    except SystemExit:
        # Let sys.exit(3) propagate normally.
        raise

    except Exception as e:
        # Shutdown sequence itself failed → dirty exit.
        logger.warning(
            "Post-order halt shutdown sequence failed.",
            extra={
                "event": "post_order_halt_shutdown_failed",
                "error_type": type(e).__name__,
                "error_message": str(e)[:200],
            },
        )
        sys.exit(4)


# ---------------------------------------------------------------------------
# run_loop — Main Entry Point
# ---------------------------------------------------------------------------

def run_loop(config_path: str = "orchestrator/config.yaml") -> None:
    """
    Main entry point for the Orchestrator. Runs the trading loop.

    Implementation note — interruptible idle wait:
      The idle wait between new-bar checks uses a threading.Event rather than
      time.sleep(). The signal handler calls event.set() when SIGINT/SIGTERM
      arrives, which immediately unblocks event.wait(timeout). This satisfies
      Requirement 4.6's 2-second shutdown target when idle, without needing
      to poll a flag in a tight loop.

    High-level algorithm:
      Phase 1 — Initialization:
        1. Load orchestrator config.
        2. Load credentials, create TradingClient (paper=True, verified).
        3. Create RestClient for data ingestion.
        4. Load strategy, risk, execution configs.
        5. Load or create state file.
        6. Check for pending deferred decision (resume if found).
        7. Run safety check.
        8. Register signal handlers (SIGINT, SIGTERM).

      Phase 2 — Main Loop (implemented in task 7.2):
        ...

      Phase 3 — Shutdown (implemented in task 7.2):
        ...

    Args:
        config_path: Path to orchestrator/config.yaml.

    Returns:
        None (exits the process via sys.exit).

    Raises:
        FileNotFoundError: If any config file is missing.
        ValueError: If any config file is invalid.
        RuntimeError: If paper-trading safety check fails.
        EnvironmentError: If credentials are missing (propagated from
                          load_credentials — Requirement 9.6).
    """
    # ==================================================================
    # Phase 1: Initialization
    # ==================================================================

    # ------------------------------------------------------------------
    # Step 1: Load orchestrator config.
    # ------------------------------------------------------------------
    # This may raise FileNotFoundError or ValueError — both are fatal
    # at startup (no config = can't run).
    orch_config = load_config(config_path)

    logger.info(
        f"Orchestrator config loaded: "
        f"symbol={orch_config.symbol}, "
        f"interval={orch_config.bar_check_interval_seconds}s, "
        f"state_file={orch_config.state_file_path}"
    )

    # ------------------------------------------------------------------
    # Step 2: Load credentials and create paper TradingClient.
    # ------------------------------------------------------------------
    # load_credentials() may raise EnvironmentError — we let it propagate
    # unmodified (Requirement 9.6).
    api_key, secret_key = load_credentials()

    # _create_paper_client() instantiates with paper=True (hard-coded) and
    # verifies the base URL.  May raise RuntimeError (Requirement 9.3).
    trading_client = _create_paper_client(api_key, secret_key)

    logger.info("Paper trading client created and verified.")

    # ------------------------------------------------------------------
    # Step 3: Create RestClient for data ingestion.
    # ------------------------------------------------------------------
    # RestClient uses StockHistoricalDataClient (market data only, read-only).
    rest_client = RestClient(api_key, secret_key)

    # ------------------------------------------------------------------
    # Step 4: Load strategy, risk, execution configs.
    # ------------------------------------------------------------------
    # Each module's load_config raises FileNotFoundError or ValueError
    # if its config file is missing or invalid — fatal at startup.
    strategy_config = load_strategy_config("strategy/config.yaml")
    risk_config = load_risk_config("risk_engine/config.yaml")
    execution_config = load_execution_config("execution/config.yaml")

    logger.info(
        f"Pipeline configs loaded: "
        f"strategy(short={strategy_config.short_window}, "
        f"long={strategy_config.long_window}), "
        f"risk_engine, execution"
    )

    # ------------------------------------------------------------------
    # Step 5: Load or create state file.
    # ------------------------------------------------------------------
    # load_state() returns empty StateData if file doesn't exist (first run),
    # or raises ValueError if JSON is corrupt.
    state_data = load_state(orch_config.state_file_path)

    logger.info(
        f"State file loaded: "
        f"positions={len(state_data.positions)}, "
        f"last_acted_bar={state_data.last_acted_bar}, "
        f"pending_decision={'yes' if state_data.pending_decision else 'no'}"
    )

    # ------------------------------------------------------------------
    # Step 6: Check for pending deferred decision.
    # ------------------------------------------------------------------
    # If the process crashed while waiting for market open, the state file
    # will contain a non-null pending_decision.  Log a WARNING and resume
    # waiting (Requirement 2.12).
    if state_data.pending_decision is not None:
        logger.warning(
            f"Found pending deferred decision in state file — "
            f"will resume waiting for market open. "
            f"Decision: {state_data.pending_decision.get('decision', '?')}, "
            f"Signal: {state_data.pending_decision.get('signal', '?')}, "
            f"Symbol: {state_data.pending_decision.get('symbol', '?')}"
        )

    # ------------------------------------------------------------------
    # Step 7: Run safety check.
    # ------------------------------------------------------------------
    # Cross-reference all positions against stop-loss orders.
    # If the API call fails, the exception propagates and halts startup
    # (Requirement 7.7 — bot SHALL NOT begin trading without confirming
    # position safety).
    try:
        safety_result = run_safety_check(trading_client)
    except Exception as exc:
        logger.critical(
            f"Safety check failed — cannot confirm position safety. "
            f"Error: {type(exc).__name__}: {str(exc)[:500]}",
            extra={
                "event": "safety_check_failed",
                "error_details": f"{type(exc).__name__}: {str(exc)[:500]}",
            },
        )
        sys.exit(1)

    if safety_result["unprotected_count"] > 0:
        logger.warning(
            f"Safety check found {safety_result['unprotected_count']} "
            f"unprotected position(s) — proceeding with caution."
        )

    # ------------------------------------------------------------------
    # Step 7b: Initialize alerting module (Telegram notifications).
    # ------------------------------------------------------------------
    # alerting.initialize() loads alerting/config.yaml and determines whether
    # Telegram delivery is enabled.  If it raises (which it shouldn't —
    # load_config catches all errors), we log a WARNING and continue with
    # alerting disabled.  Alerting failure must NEVER prevent trading.
    #
    # We pass kill_switch_file_path and a status callback for the stretch-goal
    # Telegram bot listener (/kill and /status commands).  These are optional
    # — if the bot feature is disabled in alerting config, they're unused.
    #
    # The status callback uses a mutable dict (_loop_status) shared between
    # this closure and the main loop.  The main loop updates the dict as it
    # progresses; the callback reads it at call time.  Using a dict avoids
    # Python's closure limitation where nested functions can't reassign
    # enclosing-scope variables without 'nonlocal' — but they CAN mutate
    # mutable objects like dicts.
    _loop_status = {
        "running": True,
        "cycle_number": 0,
        "last_outcome": "N/A",
    }

    def _get_loop_status():
        """Status callback for the Telegram bot /status command."""
        return {
            "running": _loop_status["running"],
            "cycle_number": _loop_status["cycle_number"],
            "last_outcome": _loop_status["last_outcome"],
            "kill_switch_active": os.path.exists(orch_config.kill_switch_file_path),
        }

    try:
        alerting.initialize(
            kill_switch_file_path=orch_config.kill_switch_file_path,
            status_callback=_get_loop_status,
        )
    except Exception as exc:
        logger.warning(
            f"Alerting module initialization failed: "
            f"{type(exc).__name__}: {str(exc)[:200]}. "
            f"Continuing with alerting disabled.",
            extra={
                "event": "alerting_init_failed",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:200],
            },
        )

    # ------------------------------------------------------------------
    # Step 8: Register signal handlers (SIGINT, SIGTERM).
    # ------------------------------------------------------------------
    # stop_flag: set to True when shutdown is requested.
    # shutdown_event: threading.Event used to interrupt idle waits.
    # cycle_in_progress: tracks whether a cycle is currently executing
    #                    (used in shutdown_initiated log event — Req 10.5).
    # cycle_start_time: tracks when the current cycle started (for 300s
    #                   timeout warning — Req 4.7).  Must be initialized
    #                   BEFORE signal handlers are registered to avoid a
    #                   race where the handler references it before binding.
    #
    # threading.Event is Python's equivalent of a condition variable in C++.
    # event.wait(timeout) blocks until either event.set() is called or the
    # timeout expires — this provides interruptible sleeping.
    stop_flag = False
    cycle_in_progress = False
    cycle_start_time = None
    cycle_number = 0
    consecutive_failures = 0
    shutdown_event = threading.Event()

    def _signal_handler(signum, frame):
        """
        Handle SIGINT (Ctrl+C) or SIGTERM gracefully.

        Sets the stop_flag and wakes the idle sleep via shutdown_event.set().
        The main loop checks stop_flag between cycles and exits cleanly.

        If a cycle is in progress and has been running > 300s, logs a
        WARNING about the potentially stuck cycle (Requirement 4.7).

        In Python, signal handlers run in the main thread.  The 'nonlocal'
        keyword (below) allows this nested function to modify the enclosing
        scope's variable — similar to a C++ lambda capturing by reference.
        """
        nonlocal stop_flag

        if stop_flag:
            # Second signal received — already shutting down.
            # Log WARNING but don't force-terminate (Requirement 4.5).
            sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
            logger.warning(
                f"Duplicate shutdown signal received ({sig_name}) — "
                f"still waiting for current cycle to finish.",
                extra={
                    "event": "duplicate_shutdown_signal",
                    "signal_name": sig_name,
                },
            )

            # Check for long-running cycle (Requirement 4.7).
            if cycle_in_progress and cycle_start_time is not None:
                elapsed = time.monotonic() - cycle_start_time
                if elapsed > 300:
                    logger.warning(
                        f"Cycle has been executing for {elapsed:.1f}s "
                        f"(> 300s) with shutdown pending.",
                        extra={
                            "event": "shutdown_cycle_timeout_warning",
                            "elapsed_seconds": elapsed,
                        },
                    )

            return

        stop_flag = True
        sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)

        logger.info(
            f"Shutdown signal received ({sig_name}). "
            f"Will exit after current cycle completes.",
            extra={
                "event": "shutdown_initiated",
                "signal": sig_name,
                "cycle_in_progress": cycle_in_progress,
            },
        )

        # If a cycle is already running > 300s, warn immediately.
        if cycle_in_progress and cycle_start_time is not None:
            elapsed = time.monotonic() - cycle_start_time
            if elapsed > 300:
                logger.warning(
                    f"Cycle has been executing for {elapsed:.1f}s "
                    f"(> 300s) with shutdown pending.",
                    extra={
                        "event": "shutdown_cycle_timeout_warning",
                        "elapsed_seconds": elapsed,
                    },
                )

        # Wake the idle sleep so we don't wait for the full polling interval.
        shutdown_event.set()

    # Register the handler for both signals.
    # signal.signal() replaces the default handler — similar to sigaction() in C.
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Signal handlers registered. Entering main loop.")

    # ==================================================================
    # Phase 2: Main Loop
    # ==================================================================
    # The main loop runs continuously until stop_flag is set (shutdown)
    # or a post-order failure halts the process.
    #
    # Loop structure:
    #   1. Check stop_flag → exit if set (between cycles only).
    #   2. If holding a deferred decision → wait for market open, then
    #      execute it DIRECTLY (not via _run_cycle — Requirement 2.9/3.8).
    #   3. Check for new daily bar.
    #   4. If new bar → run a full cycle via _run_cycle().
    #   5. Classify any errors, update state, log events.
    #   6. Sleep (interruptible via shutdown_event).

    while True:
        # --------------------------------------------------------------
        # Step 0: Kill switch check — FIRST operation, before stop_flag.
        # --------------------------------------------------------------
        # Requirement 2.1: checked at top of every loop iteration, before
        # the Stop_Flag check and before the new-bar check.
        # Requirement 2.4: If both kill switch and stop_flag are active,
        # kill switch wins because it is checked first.
        if _check_kill_switch(orch_config.kill_switch_file_path):
            _kill_switch_shutdown(orch_config, state_data, cycle_number)

        # --------------------------------------------------------------
        # Step 1: Check stop_flag → graceful shutdown.
        # --------------------------------------------------------------
        if stop_flag:
            # If holding a deferred decision, discard it (Requirement 2.11).
            if state_data.pending_decision is not None:
                logger.warning(
                    "Discarding deferred decision due to shutdown. "
                    f"Decision: {state_data.pending_decision.get('decision', '?')}, "
                    f"Symbol: {state_data.pending_decision.get('symbol', '?')}",
                )
                state_data = StateData(
                    positions=state_data.positions,
                    last_acted_bar=state_data.last_acted_bar,
                    pending_decision=None,
                    pending_decision_bar_timestamp=None,
                )

            # Flush state file atomically (Requirement 4.3).
            try:
                save_state(state_data, orch_config.state_file_path)
            except OSError as exc:
                logger.critical(
                    f"State file write failed during shutdown: {exc}",
                    extra={"event": "state_write_failed"},
                )

            logger.info(
                "Shutdown completed.",
                extra={"event": "shutdown_completed"},
            )
            _loop_status["running"] = False
            alerting.send_alert("graceful_shutdown_completed", {
                "cycle_number": cycle_number,
            })
            alerting.stop_bot()
            sys.exit(0)

        # --------------------------------------------------------------
        # Step 2: Handle deferred decision (wait for market open).
        # --------------------------------------------------------------
        # CRITICAL DESIGN POINT (Requirement 2.9/3.8):
        # When resuming a deferred decision, we call execute_decision()
        # DIRECTLY — we do NOT re-invoke _run_cycle() from the top.
        # Re-invoking _run_cycle would re-run Stages 1-4 and recompute
        # the signal, violating "SHALL NOT recompute the signal".
        if state_data.pending_decision is not None:
            # Check if market is now open.
            try:
                clock = trading_client.get_clock()
                if clock is not None and clock.is_open:
                    # Market is open — execute the deferred decision NOW.
                    logger.info(
                        "Market is open — executing deferred decision. "
                        f"Decision: {state_data.pending_decision.get('decision', '?')}, "
                        f"Symbol: {state_data.pending_decision.get('symbol', '?')}"
                    )

                    cycle_in_progress = True
                    cycle_start_time = time.monotonic()

                    try:
                        # Call execute_decision DIRECTLY (not _run_cycle).
                        order_result = execute_decision(
                            state_data.pending_decision, execution_config
                        )

                        # Contract check on execution result.
                        if not isinstance(order_result, dict):
                            raise ContractViolationError(
                                f"Stage 'execution': execute_decision() returned "
                                f"{type(order_result).__name__}, expected dict."
                            )

                        status = order_result.get("status", "")
                        cycle_number += 1

                        # Process the result.
                        if status in ("filled", "partial_fill"):
                            # Record position.
                            signal_type = state_data.pending_decision.get("signal", "")
                            if signal_type == "BUY":
                                state_data = add_position_record(
                                    state_data, order_result, orch_config.symbol
                                )
                            elif signal_type == "SELL":
                                state_data = update_position_on_sell(
                                    state_data, order_result, orch_config.symbol
                                )

                        # Determine the outcome string.
                        valid_outcomes = {
                            "filled", "partial_fill", "skipped", "cancelled",
                            "expired", "rejected", "timeout",
                        }

                        if status in valid_outcomes:
                            outcome = status
                        elif status == "error":
                            outcome = "error"
                        else:
                            # Unknown status — contract violation, same as _run_cycle.
                            raise ContractViolationError(
                                f"Stage 'execution': execute_decision() returned "
                                f"unknown status '{status}' during deferred execution. "
                                f"Expected one of: {valid_outcomes | {'error'}}."
                            )

                        # Clear pending decision and update last_acted_bar.
                        # CRITICAL: Advance last_acted_bar to the bar that
                        # TRIGGERED this deferred decision — stored in
                        # pending_decision_bar_timestamp.  Without this,
                        # the same bar would be detected as "new" again and
                        # trigger a duplicate cycle.
                        resolved_bar = state_data.pending_decision_bar_timestamp
                        state_data = StateData(
                            positions=state_data.positions,
                            last_acted_bar=resolved_bar if resolved_bar else state_data.last_acted_bar,
                            pending_decision=None,
                            pending_decision_bar_timestamp=None,
                        )
                        save_state(state_data, orch_config.state_file_path)

                        # Log cycle completed.
                        logger.info(
                            f"Deferred execution resolved: outcome={outcome}, "
                            f"symbol={orch_config.symbol}",
                            extra={
                                "event": "cycle_completed",
                                "cycle_number": cycle_number,
                                "outcome": outcome,
                                "symbol": orch_config.symbol,
                            },
                        )

                        # Handle error status from execution.
                        if status == "error":
                            classification = _classify_error(
                                "execution", RuntimeError("deferred execution error"),
                                order_result=order_result,
                            )
                            if classification == "post_order":
                                entry_id = order_result.get("entry_order_id", "unknown")
                                logger.critical(
                                    f"Loop halted: post-order failure during deferred execution. "
                                    f"entry_order_id={entry_id}, symbol={orch_config.symbol}",
                                    extra={
                                        "event": "loop_halted",
                                        "entry_order_id": entry_id,
                                        "symbol": orch_config.symbol,
                                        "error_details": order_result.get("reason", "")[:500],
                                        "cycle_number": cycle_number,
                                    },
                                )
                                _post_order_halt_shutdown(orch_config, state_data, cycle_number, {
                                    "error_details": order_result.get("reason", "")[:500],
                                    "symbol": orch_config.symbol,
                                    "entry_order_id": entry_id,
                                })

                        consecutive_failures = 0

                    except ContractViolationError as exc:
                        # Contract violation during deferred execution → halt.
                        logger.critical(
                            f"Loop halted: contract violation during deferred execution. "
                            f"Error: {exc}",
                            extra={
                                "event": "loop_halted",
                                "entry_order_id": "unknown",
                                "symbol": orch_config.symbol,
                                "error_details": str(exc)[:500],
                                "cycle_number": cycle_number,
                            },
                        )
                        _post_order_halt_shutdown(orch_config, state_data, cycle_number, {
                            "error_details": str(exc)[:500],
                            "symbol": orch_config.symbol,
                            "entry_order_id": "unknown",
                        })

                    except (TypeError, ValueError) as exc:
                        # Validation error from execute_decision → halt (raw exception
                        # from execution, can't determine if order was submitted).
                        logger.critical(
                            f"Loop halted: execution raised during deferred execution. "
                            f"Error: {type(exc).__name__}: {str(exc)[:500]}",
                            extra={
                                "event": "loop_halted",
                                "entry_order_id": "unknown",
                                "symbol": orch_config.symbol,
                                "error_details": f"{type(exc).__name__}: {str(exc)[:500]}",
                                "cycle_number": cycle_number,
                            },
                        )
                        _post_order_halt_shutdown(orch_config, state_data, cycle_number, {
                            "error_details": f"{type(exc).__name__}: {str(exc)[:500]}",
                            "symbol": orch_config.symbol,
                            "entry_order_id": "unknown",
                        })

                    finally:
                        cycle_in_progress = False
                        cycle_start_time = None

                    # Continue to check for new bars (don't sleep immediately).
                    continue

            except Exception as exc:
                # Clock check failed — log and retry next interval.
                logger.warning(
                    f"Market-open check failed while waiting for deferred execution: "
                    f"{type(exc).__name__}: {str(exc)[:500]}"
                )

            # Market not yet open (or check failed) — sleep and recheck.
            _interruptible_wait(
                shutdown_event, orch_config.bar_check_interval_seconds,
                orch_config.kill_switch_file_path,
            )
            shutdown_event.clear()
            continue

        # --------------------------------------------------------------
        # Step 3: Check for new daily bar.
        # --------------------------------------------------------------
        try:
            is_new, bar_timestamp = _check_new_bar(
                rest_client, orch_config.symbol, state_data.last_acted_bar
            )
        except Exception as exc:
            # New-bar check failed — log WARNING and retry next interval.
            logger.warning(
                f"New-bar check failed: {type(exc).__name__}: {str(exc)[:500]}",
                extra={
                    "event": "cycle_skipped",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "pipeline_stage": "data_ingestion",
                },
            )
            alerting.send_alert("skip_and_retry_error", {
                "error_summary": f"{type(exc).__name__}: {str(exc)[:200]}",
                "cycle_number": cycle_number,
            })
            _interruptible_wait(
                shutdown_event, orch_config.bar_check_interval_seconds,
                orch_config.kill_switch_file_path,
            )
            shutdown_event.clear()
            continue

        if not is_new:
            # No new bar — idle and recheck.
            _interruptible_wait(
                shutdown_event, orch_config.bar_check_interval_seconds,
                orch_config.kill_switch_file_path,
            )
            shutdown_event.clear()
            continue

        # New bar detected!
        logger.debug(
            f"New bar detected: bar_timestamp={bar_timestamp}, "
            f"previous_last_acted_bar={state_data.last_acted_bar}",
            extra={
                "event": "new_bar_detected",
                "bar_timestamp": bar_timestamp,
                "previous_last_acted_bar": state_data.last_acted_bar,
            },
        )

        # --------------------------------------------------------------
        # Step 4: Run a full pipeline cycle.
        # --------------------------------------------------------------
        cycle_number += 1
        cycle_in_progress = True
        cycle_start_time = time.monotonic()

        try:
            outcome, order_result = _run_cycle(
                rest_client=rest_client,
                trading_client=trading_client,
                strategy_config=strategy_config,
                risk_config=risk_config,
                execution_config=execution_config,
                orch_config=orch_config,
                state_data=state_data,
                cycle_number=cycle_number,
            )

        except Exception as exc:
            # --------------------------------------------------------------
            # Step 5a: Error during cycle — classify and handle.
            # --------------------------------------------------------------
            cycle_in_progress = False
            cycle_start_time = None

            classification = _classify_error(
                stage=_determine_failed_stage(exc),
                error=exc,
                order_result=None,
            )

            if classification == "post_order":
                # Halt the loop — ambiguous state or contract violation.
                logger.critical(
                    f"Loop halted: {type(exc).__name__}: {str(exc)[:500]}",
                    extra={
                        "event": "loop_halted",
                        "entry_order_id": "unknown",
                        "symbol": orch_config.symbol,
                        "error_details": f"{type(exc).__name__}: {str(exc)[:500]}",
                        "cycle_number": cycle_number,
                    },
                )
                _post_order_halt_shutdown(orch_config, state_data, cycle_number, {
                    "error_details": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "symbol": orch_config.symbol,
                    "entry_order_id": "unknown",
                })
            else:
                # Pre-order failure — skip cycle, continue.
                consecutive_failures += 1
                log_level = logging.ERROR if consecutive_failures >= 3 else logging.WARNING
                logger.log(
                    log_level,
                    f"Cycle skipped (consecutive #{consecutive_failures}): "
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                    extra={
                        "event": "cycle_skipped",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:500],
                        "pipeline_stage": _determine_failed_stage(exc),
                    },
                )
                alerting.send_alert("skip_and_retry_error", {
                    "error_summary": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "cycle_number": cycle_number,
                })
                _interruptible_wait(
                    shutdown_event, orch_config.bar_check_interval_seconds,
                    orch_config.kill_switch_file_path,
                )
                shutdown_event.clear()
                continue

        # --------------------------------------------------------------
        # Step 5b: Cycle completed successfully — process outcome.
        # --------------------------------------------------------------
        cycle_in_progress = False
        cycle_start_time = None

        # Handle "error" outcome from execution.
        if outcome == "error" and order_result is not None:
            classification = _classify_error(
                "execution", RuntimeError("execution error"), order_result=order_result
            )
            if classification == "post_order":
                entry_id = order_result.get("entry_order_id", "unknown")
                logger.critical(
                    f"Loop halted: post-order execution failure. "
                    f"entry_order_id={entry_id}, symbol={orch_config.symbol}",
                    extra={
                        "event": "loop_halted",
                        "entry_order_id": entry_id,
                        "symbol": orch_config.symbol,
                        "error_details": order_result.get("reason", "")[:500],
                        "cycle_number": cycle_number,
                    },
                )
                _post_order_halt_shutdown(orch_config, state_data, cycle_number, {
                    "error_details": order_result.get("reason", "")[:500],
                    "symbol": orch_config.symbol,
                    "entry_order_id": entry_id,
                })
            else:
                # Pre-order execution error — skip.
                consecutive_failures += 1
                log_level = logging.ERROR if consecutive_failures >= 3 else logging.WARNING
                logger.log(
                    log_level,
                    f"Cycle skipped (execution pre-order error, "
                    f"consecutive #{consecutive_failures}): "
                    f"{order_result.get('reason', 'unknown')[:500]}",
                    extra={
                        "event": "cycle_skipped",
                        "error_type": order_result.get("error_type", "unknown"),
                        "error_message": order_result.get("reason", "")[:500],
                        "pipeline_stage": "execution",
                    },
                )
                alerting.send_alert("skip_and_retry_error", {
                    "error_summary": f"{order_result.get('reason', 'unknown')[:200]}",
                    "cycle_number": cycle_number,
                })
                _interruptible_wait(
                    shutdown_event, orch_config.bar_check_interval_seconds,
                    orch_config.kill_switch_file_path,
                )
                shutdown_event.clear()
                continue

        # Handle "deferred" outcome (market closed).
        if outcome == "deferred":
            # Persist the Decision_Dict AND the bar_timestamp to state file
            # (Requirement 2.12).  The bar_timestamp is needed so that when
            # the deferred decision resolves, we can advance last_acted_bar
            # to this value — preventing duplicate cycles for the same bar.
            state_data = StateData(
                positions=state_data.positions,
                last_acted_bar=state_data.last_acted_bar,  # NOT updated until resolved.
                pending_decision=order_result,  # order_result is the Decision_Dict here.
                pending_decision_bar_timestamp=bar_timestamp,  # Store which bar triggered this.
            )
            save_state(state_data, orch_config.state_file_path)
            logger.info(
                f"Execution deferred (market closed). Decision persisted to state file. "
                f"Symbol={orch_config.symbol}, bar={bar_timestamp}",
            )
            # Loop will pick up the deferred decision in Step 2 next iteration.
            continue

        # Handle successful outcomes (filled, partial_fill, vetoed, no_action, etc.)
        # Update position records for fills.
        if outcome in ("filled", "partial_fill") and order_result is not None:
            # Alert: order was filled/partially filled (Requirement 6.3).
            alerting.send_alert("order_filled", {
                "symbol": orch_config.symbol,
                "order_id": order_result.get("entry_order_id", "N/A"),
                "filled_quantity": order_result.get("filled_quantity", "N/A"),
                "fill_price": order_result.get("fill_price", "N/A"),
            })

            signal_type = order_result.get("signal", "")
            if signal_type == "BUY":
                state_data = add_position_record(
                    state_data, order_result, orch_config.symbol
                )
            elif signal_type == "SELL":
                state_data = update_position_on_sell(
                    state_data, order_result, orch_config.symbol
                )

        # Alert: order was submitted to Alpaca (Requirement 6.4).
        # Fires for ANY outcome where an order was actually placed (entry_order_id
        # is not None).  The execution module is synchronous — it submits and waits
        # for a terminal state — so by the time we see the Order_Result, the order
        # has already been submitted.  This alert fires for filled, partial_fill,
        # cancelled, expired, rejected, timeout — any case where entry_order_id
        # proves submission occurred.
        if order_result is not None and order_result.get("entry_order_id") is not None:
            alerting.send_alert("order_submitted", {
                "symbol": orch_config.symbol,
                "order_id": order_result.get("entry_order_id", "N/A"),
            })

        # Update last_acted_bar (Requirement 2.6 — only after resolution).
        state_data = StateData(
            positions=state_data.positions,
            last_acted_bar=bar_timestamp,
            pending_decision=state_data.pending_decision,
        )

        # Flush state file.
        try:
            save_state(state_data, orch_config.state_file_path)
        except OSError as exc:
            # State file write failure → halt (Requirement 6.9).
            logger.critical(
                f"State file write failed: {exc}. Halting.",
                extra={"event": "state_write_failed"},
            )
            _post_order_halt_shutdown(orch_config, state_data, cycle_number, {
                "error_details": f"State file write failed: {str(exc)[:200]}",
                "symbol": orch_config.symbol,
                "entry_order_id": "N/A",
            })

        # Log cycle completed.
        logger.info(
            f"Cycle #{cycle_number} completed: outcome={outcome}, "
            f"symbol={orch_config.symbol}",
            extra={
                "event": "cycle_completed",
                "cycle_number": cycle_number,
                "outcome": outcome,
                "symbol": orch_config.symbol,
            },
        )

        # Reset consecutive failure counter on successful cycle.
        consecutive_failures = 0

        # Update loop status for Telegram bot /status command.
        _loop_status["cycle_number"] = cycle_number
        _loop_status["last_outcome"] = outcome

        # Brief pause before next iteration (don't busy-loop).
        # The _interruptible_wait allows both shutdown and kill switch to
        # wake the loop early.
        _interruptible_wait(
            shutdown_event, orch_config.bar_check_interval_seconds,
            orch_config.kill_switch_file_path,
        )
        shutdown_event.clear()


# ---------------------------------------------------------------------------
# _determine_failed_stage — helper for error classification
# ---------------------------------------------------------------------------

def _determine_failed_stage(exc: Exception) -> str:
    """
    Attempt to determine which pipeline stage raised the exception based on
    the exception type and message.

    This is a best-effort heuristic used when _run_cycle raises an exception
    (rather than returning an error outcome).  The classification is used by
    _classify_error to determine pre_order vs post_order.

    Args:
        exc: The exception that was raised.

    Returns:
        One of: "account_query", "data_ingestion", "strategy", "risk_engine",
                "execution".  Defaults to "execution" (conservative — halt)
                if the stage cannot be determined.
    """
    # ContractViolationError messages contain the stage name.
    msg = str(exc).lower()

    if isinstance(exc, ContractViolationError):
        if "account_query" in msg:
            return "account_query"
        elif "data_ingestion" in msg:
            return "data_ingestion"
        elif "strategy" in msg:
            return "strategy"
        elif "risk_engine" in msg:
            return "risk_engine"
        elif "execution" in msg:
            return "execution"
        # ContractViolationError without stage → conservative halt.
        return "execution"

    # ValueError from format_account_snapshot (equity/cash check).
    if isinstance(exc, ValueError) and ("equity" in msg or "cash" in msg):
        return "account_query"

    # IOError typically from RestClient (Data Ingestion).
    if isinstance(exc, IOError):
        return "data_ingestion"

    # Default: conservative choice is "execution" (will halt).
    # This catches raw exceptions from any stage where we can't determine
    # the origin — safer to halt than to incorrectly retry.
    return "execution"
