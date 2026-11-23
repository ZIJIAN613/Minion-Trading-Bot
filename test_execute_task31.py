"""Quick validation test for Task 3.1 — execute.py implementation."""
import pandas as pd
from execution import execute_decision
from execution.config_loader import ExecutionConfig

config = ExecutionConfig(fill_timeout_seconds=30.0, poll_interval_seconds=1.0)

# Test 1: VETOED decision
vetoed = {
    "decision": "VETOED",
    "signal": "BUY",
    "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
    "reason": "Daily loss limit reached",
    "symbol": "AAPL",
}
result = execute_decision(vetoed, config)
assert result["status"] == "skipped"
assert result["decision"] == "VETOED"
assert result["position_size"] == 0
assert result["reason"] == "Decision was VETOED by risk engine"
assert result["symbol"] == "AAPL"
assert result["signal"] == "BUY"
assert result["timestamp"] == pd.Timestamp("2024-01-01", tz="UTC")
print("Test 1 PASSED: VETOED returns skipped")

# Test 2: NO_ACTION decision
no_action = {
    "decision": "NO_ACTION",
    "signal": "HOLD",
    "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
    "reason": "HOLD signals require no risk evaluation",
}
result = execute_decision(no_action, config)
assert result["status"] == "skipped"
assert result["decision"] == "NO_ACTION"
assert result["position_size"] == 0
assert result["reason"] == "No action required (HOLD signal)"
assert result["symbol"] is None
print("Test 2 PASSED: NO_ACTION returns skipped")

# Test 3: APPROVED+HOLD guard
hold = {
    "decision": "APPROVED",
    "signal": "HOLD",
    "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
    "reason": "Some reason",
    "position_size": 10,
    "stop_loss": 150.0,
    "symbol": "AAPL",
}
result = execute_decision(hold, config)
assert result["status"] == "skipped"
assert result["reason"] == "HOLD signal cannot be executed"
print("Test 3 PASSED: APPROVED+HOLD returns skipped")

# Test 4: APPROVED BUY with a client that raises returns error result
# Now that order submission is wired, we use a mock client that raises
# to verify the error wrapping catches exceptions (Requirement 12.4).
class ErrorClient:
    """Mock client that raises on submit_order to test error wrapping."""
    def submit_order(self, *args, **kwargs):
        raise RuntimeError("Mock submission failure")
    def get_order_by_id(self, *args, **kwargs):
        raise RuntimeError("Mock query failure")
    def cancel_order_by_id(self, *args, **kwargs):
        raise RuntimeError("Mock cancel failure")

buy = {
    "decision": "APPROVED",
    "signal": "BUY",
    "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
    "reason": "All risk checks passed",
    "position_size": 10,
    "stop_loss": 145.0,
    "symbol": "AAPL",
}
result = execute_decision(buy, config, client=ErrorClient())
assert result["status"] == "error"
assert result["error_type"] == "RuntimeError"
assert result["entry_order_id"] is None  # Error happened before submission
print("Test 4 PASSED: APPROVED BUY with failing client returns error")

# Test 5: TypeError for non-dict
try:
    execute_decision(None, config)
    assert False, "Should have raised TypeError"
except TypeError as e:
    assert "NoneType" in str(e)
    print("Test 5 PASSED: None raises TypeError")

# Test 6: ValueError for missing decision key
try:
    execute_decision({}, config)
    assert False, "Should have raised ValueError"
except ValueError as e:
    assert "decision" in str(e)
    print("Test 6 PASSED: Empty dict raises ValueError")

# Test 7: VETOED without symbol — symbol should be None
vetoed_no_symbol = {
    "decision": "VETOED",
    "signal": "SELL",
    "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
    "reason": "Max positions reached",
}
result = execute_decision(vetoed_no_symbol, config)
assert result["status"] == "skipped"
assert result["symbol"] is None
print("Test 7 PASSED: VETOED without symbol returns None for symbol")

# Test 8: NO_ACTION with symbol present
no_action_sym = {
    "decision": "NO_ACTION",
    "signal": "HOLD",
    "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
    "reason": "HOLD signals require no risk evaluation",
    "symbol": "MSFT",
}
result = execute_decision(no_action_sym, config)
assert result["symbol"] == "MSFT"
print("Test 8 PASSED: NO_ACTION with symbol preserves it")

# Test 9: Verify client parameter is not used for VETOED (no API interaction)
class FakeClient:
    """A fake client that explodes if any method is called."""
    def __getattr__(self, name):
        raise AssertionError(f"Client method '{name}' was called for non-approved!")

result = execute_decision(vetoed, config, client=FakeClient())
assert result["status"] == "skipped"
print("Test 9 PASSED: Client NOT invoked for VETOED decision")

result = execute_decision(no_action, config, client=FakeClient())
assert result["status"] == "skipped"
print("Test 10 PASSED: Client NOT invoked for NO_ACTION decision")

print()
print("ALL TESTS PASSED")
