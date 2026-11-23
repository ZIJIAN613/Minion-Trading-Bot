# tests/test_execution_properties.py
#
# Property-based tests for the execution module using Hypothesis.
# Each test function validates one correctness property from the design doc.
#
# CONVENTION: All Hypothesis property tests use @settings(max_examples=100)
# as a minimum to ensure good coverage without excessive test time.
#
# For C++ developers: Hypothesis is like QuickCheck or RapidCheck — it
# generates random inputs according to declared "strategies" (generators)
# and verifies that properties hold for ALL generated inputs.

import sys
import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# hypothesis is the property-based testing library (like QuickCheck for Python).
# @given decorates a test, telling Hypothesis what to generate.
# @settings configures test parameters (max_examples, etc.).
# st (strategies) is a module of "generator" combinators.
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

# Module under test.
from execution.execute import execute_decision, _submit_buy_bracket, _submit_sell_market
from execution.config_loader import ExecutionConfig, load_config
from execution.validators import validate_decision_dict


# ---------------------------------------------------------------------------
# Shared fixtures and strategies
# ---------------------------------------------------------------------------

# A valid ExecutionConfig for use in property tests.
# Values are within range: fill_timeout_seconds in [5,300], poll_interval in [0.5,30].
VALID_CONFIG = ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=5)


def _utc_timestamp():
    """Return a UTC-aware pandas Timestamp for test use."""
    return pd.Timestamp("2024-06-15 12:00:00", tz="UTC")


# Strategy: generate non-dict values of various types.
# st.one_of() picks from multiple strategies — like a union of generators.
# st.none() generates None, st.integers() generates ints, etc.
non_dict_strategy = st.one_of(
    st.none(),
    st.integers(),
    st.text(min_size=0, max_size=10),
    st.lists(st.integers(), max_size=3),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.tuples(st.integers(), st.integers()),
)


# Strategy: generate valid symbols (1-5 uppercase letters).
# st.text() with a specific alphabet generates strings from those chars.
symbol_strategy = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    min_size=1,
    max_size=5,
)

# Strategy: valid position sizes (integers from 1 to 100).
position_size_strategy = st.integers(min_value=1, max_value=100)

# Strategy: valid stop_loss prices (floats from 1.0 to 1000.0).
stop_loss_strategy = st.floats(min_value=1.0, max_value=1000.0, allow_nan=False)

# Strategy: valid reason strings (1-100 chars, printable ASCII).
reason_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=100,
)


# ---------------------------------------------------------------------------
# Property 1: Non-dict input raises TypeError with type name
# ---------------------------------------------------------------------------
# Feature: execution, Property 1: Non-dict input raises TypeError with type name
# **Validates: Requirements 1.1**

@given(value=non_dict_strategy)
@settings(max_examples=100)
def test_property_1_non_dict_raises_typeerror(value):
    """
    For any value that is not a dict, execute_decision SHALL raise TypeError
    whose message contains the name of the actual type received.
    """
    # We pass any valid config since validation should fail before it's used.
    config = VALID_CONFIG

    # pytest.raises() is a context manager that asserts the enclosed code
    # raises the specified exception — like EXPECT_THROW in Google Test.
    with pytest.raises(TypeError) as exc_info:
        execute_decision(value, config)

    # The error message must contain the type name of the value.
    # type(x).__name__ gives the class name as a string.
    type_name = type(value).__name__
    assert type_name in str(exc_info.value), (
        f"TypeError message should mention '{type_name}', "
        f"but got: '{exc_info.value}'"
    )


# ---------------------------------------------------------------------------
# Property 2: Validation order is strictly sequential (fail-fast)
# ---------------------------------------------------------------------------
# Feature: execution, Property 2: Validation order is strictly sequential
# **Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6, 1.7**

@given(step=st.integers(min_value=2, max_value=8))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_validation_order_fail_fast(step):
    """
    For a dict that is valid up to step N-1 but invalid at step N,
    the correct error is raised for step N and no later step is checked.

    Steps:
      2 = invalid/missing 'decision'
      3 = invalid/missing 'signal'
      4 = invalid/missing 'timestamp'
      5 = invalid/missing 'reason'
      6 = (APPROVED) invalid 'position_size'
      7 = (APPROVED) invalid 'stop_loss'
      8 = (APPROVED) invalid 'symbol'
    """
    config = VALID_CONFIG

    # Build a dict that is valid up to step-1, broken at step.
    # Start with a fully valid APPROVED dict.
    d = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": _utc_timestamp(),
        "reason": "Test reason",
        "position_size": 10,
        "stop_loss": 150.0,
        "symbol": "AAPL",
    }

    # Now break the field corresponding to step N.
    if step == 2:
        # Break 'decision' — use an invalid value.
        d["decision"] = "INVALID_DECISION"
    elif step == 3:
        # Break 'signal' — use an invalid value.
        d["signal"] = "INVALID_SIGNAL"
    elif step == 4:
        # Break 'timestamp' — use a non-Timestamp value.
        d["timestamp"] = "not-a-timestamp"
    elif step == 5:
        # Break 'reason' — use empty string (below 1 char min).
        d["reason"] = ""
    elif step == 6:
        # Break 'position_size' — use a boolean (explicitly rejected).
        d["position_size"] = True
    elif step == 7:
        # Break 'stop_loss' — use a boolean (explicitly rejected).
        d["stop_loss"] = False
    elif step == 8:
        # Break 'symbol' — use empty string (below 1 char min).
        d["symbol"] = ""

    # All steps should raise ValueError (step 1 is TypeError, tested above).
    with pytest.raises(ValueError) as exc_info:
        execute_decision(d, config)

    # Verify the error message mentions the broken field, not a later one.
    error_msg = str(exc_info.value).lower()

    # Map steps to the key name that should appear in the error.
    step_to_key = {
        2: "decision",
        3: "signal",
        4: "timestamp",
        5: "reason",
        6: "position_size",
        7: "stop_loss",
        8: "symbol",
    }

    expected_key = step_to_key[step]
    assert expected_key in error_msg, (
        f"For step {step}, error should mention '{expected_key}', "
        f"but got: '{exc_info.value}'"
    )


# ---------------------------------------------------------------------------
# Property 3: Non-approved decisions return skipped with no API interaction
# ---------------------------------------------------------------------------
# Feature: execution, Property 3: Non-approved decisions return skipped
# **Validates: Requirements 2.1, 2.2, 2.3**

@given(
    decision=st.sampled_from(["VETOED", "NO_ACTION"]),
    signal=st.sampled_from(["BUY", "SELL", "HOLD"]),
    symbol=st.one_of(st.none(), symbol_strategy),
    reason=reason_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_non_approved_returns_skipped(decision, signal, symbol, reason):
    """
    For any valid VETOED/NO_ACTION Decision_Dict, execute_decision returns
    status="skipped" with position_size=0 and echoes back the original
    decision/signal/timestamp — without invoking the Alpaca client.
    """
    # assume() tells Hypothesis to skip examples that don't meet a precondition.
    # We need reason to be non-empty (1-500 chars).
    assume(len(reason) >= 1 and len(reason) <= 500)

    ts = _utc_timestamp()
    config = VALID_CONFIG

    # Build a valid non-approved Decision_Dict.
    # For VETOED/NO_ACTION, position_size and stop_loss are not required,
    # but symbol is optional.
    d = {
        "decision": decision,
        "signal": signal,
        "timestamp": ts,
        "reason": reason,
    }
    if symbol is not None:
        d["symbol"] = symbol

    # Create a MagicMock as the Alpaca client.
    # MagicMock records all calls made on it — like a test spy in Google Mock.
    mock_client = MagicMock()

    result = execute_decision(d, config, client=mock_client)

    # Verify returned Order_Result structure.
    assert result["status"] == "skipped"
    assert result["decision"] == decision
    assert result["signal"] == signal
    assert result["position_size"] == 0
    assert result["timestamp"] == ts

    # Verify the client was NEVER called (Requirement 2.3).
    # .assert_not_called() would only check the mock itself, but we need
    # to verify submit_order specifically was not called.
    mock_client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Property 4: BUY OTO order construction
# ---------------------------------------------------------------------------
# Feature: execution, Property 4: BUY bracket order construction
# **Validates: Requirements 3.1, 3.3, 3.4, 4.1, 4.2**

@given(
    symbol=symbol_strategy,
    position_size=position_size_strategy,
    stop_loss=stop_loss_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_property_4_buy_oto_order_construction(symbol, position_size, stop_loss, monkeypatch):
    """
    For any valid APPROVED BUY Decision_Dict, _submit_buy_bracket is called
    with the correct symbol, quantity, and stop_loss_price.
    """
    ts = _utc_timestamp()
    config = VALID_CONFIG

    d = {
        "decision": "APPROVED",
        "signal": "BUY",
        "timestamp": ts,
        "reason": "Buy signal approved",
        "position_size": position_size,
        "stop_loss": stop_loss,
        "symbol": symbol,
    }

    # Capture the arguments passed to _submit_buy_bracket.
    # We monkeypatch it to record what was passed, then return a fake result.
    captured_args = {}

    def fake_submit_buy_bracket(client, sym, qty, stop_price):
        captured_args["symbol"] = sym
        captured_args["quantity"] = qty
        captured_args["stop_loss_price"] = stop_price
        return {
            "entry_order_id": "test-order-id",
            "stop_loss_order_id": "test-stop-id",
            "bracket_fallback": False,
        }

    # monkeypatch.setattr replaces a module attribute at runtime — reverted
    # automatically after the test finishes.  Like a C++ test fixture that
    # injects a mock dependency.
    import execution.execute as exe_module
    monkeypatch.setattr(exe_module, "_submit_buy_bracket", fake_submit_buy_bracket)

    # Also monkeypatch _poll_fill to return immediately.
    def fake_poll_fill(client, order_id, cfg):
        return {
            "status": "filled",
            "fill_price": 100.0,
            "filled_quantity": position_size,
            "filled_at": pd.Timestamp.now("UTC"),
        }
    monkeypatch.setattr(exe_module, "_poll_fill", fake_poll_fill)

    mock_client = MagicMock()
    execute_decision(d, config, client=mock_client)

    # Verify correct arguments passed to _submit_buy_bracket.
    assert captured_args["symbol"] == symbol
    assert captured_args["quantity"] == position_size
    assert captured_args["stop_loss_price"] == stop_loss


# ---------------------------------------------------------------------------
# Property 5: SELL plain market order construction
# ---------------------------------------------------------------------------
# Feature: execution, Property 5: SELL plain market order construction
# **Validates: Requirements 3.2, 3.7, 4.7**

@given(
    symbol=symbol_strategy,
    position_size=position_size_strategy,
    stop_loss=stop_loss_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_property_5_sell_plain_market_order(symbol, position_size, stop_loss, monkeypatch):
    """
    For any valid APPROVED SELL Decision_Dict, _submit_sell_market is called
    with the correct symbol and quantity.  _submit_buy_bracket is NOT called.
    The stop_loss value from the Decision_Dict is NOT passed to _submit_sell_market
    (it only takes 3 args: client, symbol, quantity).
    """
    ts = _utc_timestamp()
    config = VALID_CONFIG

    d = {
        "decision": "APPROVED",
        "signal": "SELL",
        "timestamp": ts,
        "reason": "Sell signal approved",
        "position_size": position_size,
        "stop_loss": stop_loss,
        "symbol": symbol,
    }

    # Track calls to both submission helpers.
    sell_captured = {}
    buy_bracket_called = False

    def fake_submit_sell_market(client, sym, qty):
        sell_captured["symbol"] = sym
        sell_captured["quantity"] = qty
        return {
            "entry_order_id": "sell-order-id",
            "stop_loss_order_id": None,
            "bracket_fallback": False,
        }

    def fake_submit_buy_bracket(client, sym, qty, stop_price):
        nonlocal buy_bracket_called
        buy_bracket_called = True
        return {
            "entry_order_id": "buy-order-id",
            "stop_loss_order_id": "stop-id",
            "bracket_fallback": False,
        }

    import execution.execute as exe_module
    monkeypatch.setattr(exe_module, "_submit_sell_market", fake_submit_sell_market)
    monkeypatch.setattr(exe_module, "_submit_buy_bracket", fake_submit_buy_bracket)

    # Monkeypatch _poll_fill to return filled immediately.
    def fake_poll_fill(client, order_id, cfg):
        return {
            "status": "filled",
            "fill_price": 100.0,
            "filled_quantity": position_size,
            "filled_at": pd.Timestamp.now("UTC"),
        }
    monkeypatch.setattr(exe_module, "_poll_fill", fake_poll_fill)

    mock_client = MagicMock()
    execute_decision(d, config, client=mock_client)

    # Verify _submit_sell_market was called with correct args.
    assert sell_captured["symbol"] == symbol
    assert sell_captured["quantity"] == position_size

    # Verify _submit_buy_bracket was NOT called.
    assert buy_bracket_called is False


# ---------------------------------------------------------------------------
# Property 6: Order_Result structure invariant
# ---------------------------------------------------------------------------
# Feature: execution, Property 6: Order_Result structure invariant
# **Validates: Requirements 11.1, 11.4**

@given(
    decision=st.sampled_from(["APPROVED", "VETOED", "NO_ACTION"]),
    signal=st.sampled_from(["BUY", "SELL", "HOLD"]),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_property_6_order_result_structure_invariant(decision, signal, monkeypatch):
    """
    For any invocation that passes validation, the returned dict always has
    base keys {status, decision, signal, symbol, position_size, reason, timestamp},
    status is one of the 8 defined values, reason is 1-500 chars, and
    timestamp is a UTC-aware pd.Timestamp.
    """
    # The 8 valid status values.
    VALID_STATUSES = {
        "filled", "partial_fill", "cancelled", "expired",
        "rejected", "timeout", "skipped", "error",
    }

    ts = _utc_timestamp()
    config = VALID_CONFIG

    # Build a valid decision dict based on the decision type.
    d = {
        "decision": decision,
        "signal": signal,
        "timestamp": ts,
        "reason": "Test reason for property 6",
    }

    if decision == "APPROVED":
        # APPROVED requires position_size, stop_loss, and symbol.
        # If signal is HOLD, execute_decision returns skipped without needing
        # the Alpaca client.
        d["position_size"] = 10
        d["stop_loss"] = 150.0
        d["symbol"] = "AAPL"

    # For APPROVED + non-HOLD signals, we need to mock the execution path.
    import execution.execute as exe_module

    if decision == "APPROVED" and signal != "HOLD":
        # Monkeypatch submission helpers and poll to return filled.
        def fake_submit(client, sym, qty, *args):
            return {
                "entry_order_id": "order-123",
                "stop_loss_order_id": "stop-123" if signal == "BUY" else None,
                "bracket_fallback": False,
            }

        def fake_sell(client, sym, qty):
            return {
                "entry_order_id": "order-123",
                "stop_loss_order_id": None,
                "bracket_fallback": False,
            }

        def fake_poll(client, order_id, cfg):
            return {
                "status": "filled",
                "fill_price": 155.0,
                "filled_quantity": 10,
                "filled_at": pd.Timestamp.now("UTC"),
            }

        monkeypatch.setattr(exe_module, "_submit_buy_bracket", fake_submit)
        monkeypatch.setattr(exe_module, "_submit_sell_market", fake_sell)
        monkeypatch.setattr(exe_module, "_poll_fill", fake_poll)

    mock_client = MagicMock()
    result = execute_decision(d, config, client=mock_client)

    # Verify base keys are always present.
    base_keys = {"status", "decision", "signal", "symbol", "position_size", "reason", "timestamp"}
    for key in base_keys:
        assert key in result, f"Missing base key '{key}' in Order_Result"

    # Verify status is one of the 8 defined values.
    assert result["status"] in VALID_STATUSES, (
        f"status '{result['status']}' is not one of the 8 defined values"
    )

    # Verify reason is a non-empty string of 1-500 characters.
    assert isinstance(result["reason"], str)
    assert 1 <= len(result["reason"]) <= 500, (
        f"reason length {len(result['reason'])} is outside [1, 500]"
    )

    # Verify timestamp is a UTC-aware pandas Timestamp.
    assert isinstance(result["timestamp"], pd.Timestamp)
    assert result["timestamp"].tzinfo is not None, (
        "timestamp must be UTC-aware (tzinfo should not be None)"
    )


# ---------------------------------------------------------------------------
# Property 7: Config validation rejects invalid values
# ---------------------------------------------------------------------------
# Feature: execution, Property 7: Config validation rejects invalid values
# **Validates: Requirements 8.2, 8.6, 8.7**

# Strategy for generating invalid config scenarios.
# Each tuple is (key_to_break, invalid_value).
invalid_config_strategy = st.one_of(
    # fill_timeout_seconds outside [5, 300]
    st.tuples(
        st.just("fill_timeout_seconds"),
        st.one_of(
            st.floats(max_value=4.9, allow_nan=False, allow_infinity=False),
            st.floats(min_value=300.1, max_value=10000.0, allow_nan=False, allow_infinity=False),
            st.just(True),          # Booleans rejected
            st.just(False),
            st.just("thirty"),      # Non-numeric
        ),
    ),
    # poll_interval_seconds outside [0.5, 30]
    st.tuples(
        st.just("poll_interval_seconds"),
        st.one_of(
            st.floats(max_value=0.4, allow_nan=False, allow_infinity=False),
            st.floats(min_value=30.1, max_value=1000.0, allow_nan=False, allow_infinity=False),
            st.just(True),
            st.just(False),
            st.just("one"),
        ),
    ),
    # Missing keys
    st.tuples(
        st.just("__missing_fill_timeout__"),
        st.just(None),
    ),
    st.tuples(
        st.just("__missing_poll_interval__"),
        st.just(None),
    ),
)


@given(data=invalid_config_strategy)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_property_7_config_validation_rejects_invalid(data, tmp_path):
    """
    For any config where values are outside valid ranges, booleans, non-numeric,
    or missing keys, load_config raises ValueError with the key name in the message.
    """
    import yaml

    key_to_break, invalid_value = data

    # Build a config dict — start with valid values, then break one.
    config_dict = {
        "fill_timeout_seconds": 30,
        "poll_interval_seconds": 1.0,
    }

    if key_to_break == "__missing_fill_timeout__":
        # Remove the key entirely.
        del config_dict["fill_timeout_seconds"]
        expected_key_in_msg = "fill_timeout_seconds"
    elif key_to_break == "__missing_poll_interval__":
        del config_dict["poll_interval_seconds"]
        expected_key_in_msg = "poll_interval_seconds"
    else:
        config_dict[key_to_break] = invalid_value
        expected_key_in_msg = key_to_break

    # Write the config to a temporary YAML file.
    # tmp_path is a pytest fixture providing a unique temp directory.
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_dict), encoding="utf-8")

    # load_config should raise ValueError.
    with pytest.raises(ValueError) as exc_info:
        load_config(str(config_file))

    # Verify the error message mentions the broken key.
    assert expected_key_in_msg in str(exc_info.value), (
        f"Expected '{expected_key_in_msg}' in error message, "
        f"but got: '{exc_info.value}'"
    )


# ---------------------------------------------------------------------------
# Property 8: Unexpected exceptions caught and returned as error
# ---------------------------------------------------------------------------
# Feature: execution, Property 8: Unexpected exceptions caught as error
# **Validates: Requirements 12.4**

@given(
    signal=st.sampled_from(["BUY", "SELL"]),
    exc_type=st.sampled_from([RuntimeError, ValueError, OSError, ConnectionError]),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_8_exceptions_caught_as_error(signal, exc_type):
    """
    For any valid APPROVED Decision_Dict where the client raises an exception,
    execute_decision returns status="error" — the exception is never propagated.
    """
    ts = _utc_timestamp()
    config = VALID_CONFIG

    d = {
        "decision": "APPROVED",
        "signal": signal,
        "timestamp": ts,
        "reason": "Testing error handling",
        "position_size": 10,
        "stop_loss": 150.0,
        "symbol": "AAPL",
    }

    # Mock client whose submit_order raises the given exception type.
    mock_client = MagicMock()
    mock_client.submit_order.side_effect = exc_type("Simulated failure")

    # execute_decision should NOT propagate the exception.
    # It should catch it and return an error result.
    result = execute_decision(d, config, client=mock_client)

    assert result["status"] == "error", (
        f"Expected status='error' but got '{result['status']}'"
    )
    assert result["error_type"] == exc_type.__name__, (
        f"Expected error_type='{exc_type.__name__}' but got '{result['error_type']}'"
    )


# ---------------------------------------------------------------------------
# Property 9: entry_order_id reflects submission state
# ---------------------------------------------------------------------------
# Feature: execution, Property 9: entry_order_id reflects submission state
# **Validates: Requirements 12.7**

@given(signal=st.sampled_from(["BUY", "SELL"]))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_property_9_entry_order_id_reflects_state(signal, monkeypatch):
    """
    - If the error occurs BEFORE submission (submit_order raises),
      entry_order_id is None.
    - If the error occurs AFTER submission (poll raises),
      entry_order_id is non-None.
    """
    import execution.execute as exe_module

    ts = _utc_timestamp()
    config = VALID_CONFIG

    d = {
        "decision": "APPROVED",
        "signal": signal,
        "timestamp": ts,
        "reason": "Testing entry_order_id state",
        "position_size": 10,
        "stop_loss": 150.0,
        "symbol": "AAPL",
    }

    # --- Case 1: Error BEFORE submission → entry_order_id is None ---
    # Use a mock client where submit_order raises immediately.
    # We need to ensure _submit_buy_bracket and _submit_sell_market are
    # using the REAL implementation so they call client.submit_order.
    # We use a fresh MonkeyPatch context to avoid cross-contamination.
    mp1 = pytest.MonkeyPatch()
    try:
        # Ensure the real submit helpers are in place for Case 1.
        # (No patching needed — just use a mock client that raises.)
        mock_client_before = MagicMock()
        mock_client_before.submit_order.side_effect = RuntimeError("Connection refused")

        result_before = execute_decision(d, config, client=mock_client_before)

        assert result_before["status"] == "error"
        assert result_before["entry_order_id"] is None, (
            "entry_order_id should be None when error occurs before submission"
        )
    finally:
        mp1.undo()

    # --- Case 2: Error AFTER submission → entry_order_id is non-None ---
    # Monkeypatch submission helpers to succeed, but _poll_fill to raise.
    mp2 = pytest.MonkeyPatch()
    try:
        def fake_poll_raises(client, order_id, cfg):
            raise RuntimeError("Unexpected poll failure")

        def fake_submit_buy(client, sym, qty, stop_price):
            return {
                "entry_order_id": "order-after-submit",
                "stop_loss_order_id": "stop-123",
                "bracket_fallback": False,
            }

        def fake_submit_sell(client, sym, qty):
            return {
                "entry_order_id": "order-after-submit",
                "stop_loss_order_id": None,
                "bracket_fallback": False,
            }

        mp2.setattr(exe_module, "_poll_fill", fake_poll_raises)
        mp2.setattr(exe_module, "_submit_buy_bracket", fake_submit_buy)
        mp2.setattr(exe_module, "_submit_sell_market", fake_submit_sell)

        mock_client_after = MagicMock()
        result_after = execute_decision(d, config, client=mock_client_after)

        assert result_after["status"] == "error"
        assert result_after["entry_order_id"] is not None, (
            "entry_order_id should be non-None when error occurs after submission"
        )
        assert result_after["entry_order_id"] == "order-after-submit"
    finally:
        mp2.undo()
