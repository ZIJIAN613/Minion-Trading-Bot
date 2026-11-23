"""
tests/test_state_timestamp_roundtrip.py
========================================
Unit test verifying that a Decision_Dict containing a pandas Timestamp
field can be round-tripped through save_state → load_state without error,
and that the resulting pending_decision's timestamp is reconverted to a
timezone-aware pd.Timestamp suitable for execute_decision().

This test was written to cover the bug where json.dumps() crashed with
"TypeError: Object of type Timestamp is not JSON serializable" when
persisting a deferred decision to state.json.
"""

import os

import pandas as pd
import pytest

from orchestrator.state_manager import (
    StateData,
    load_state,
    save_state,
)


class TestTimestampRoundTrip:
    """
    Tests for the Timestamp serialization fix in save_state/load_state.
    """

    def test_save_state_handles_pandas_timestamp(self, tmp_path):
        """
        save_state SHALL NOT crash when pending_decision contains a
        pandas Timestamp — it must serialize it to an ISO 8601 string.
        """
        # Build a Decision_Dict with a real pandas Timestamp, exactly
        # as strategy.compute_signal() and risk_engine.evaluate_signal()
        # would produce.
        decision = {
            "decision": "APPROVED",
            "signal": "BUY",
            "position_size": 10,
            "stop_loss": 300.0,
            "reason": "All risk checks passed",
            "timestamp": pd.Timestamp("2026-08-21T04:00:00", tz="UTC"),
            "symbol": "AAPL",
        }

        state = StateData(
            positions=[],
            last_acted_bar="2026-08-20T04:00:00Z",
            pending_decision=decision,
            pending_decision_bar_timestamp="2026-08-21T04:00:00Z",
        )

        state_path = str(tmp_path / "state.json")

        # This used to raise: TypeError: Object of type Timestamp is not JSON serializable
        save_state(state, state_path)

        # Verify file was actually written.
        assert os.path.exists(state_path)

    def test_roundtrip_preserves_timestamp_as_pd_timestamp(self, tmp_path):
        """
        After save_state → load_state, the pending_decision's 'timestamp'
        field SHALL be a timezone-aware pd.Timestamp, so it passes
        execution.validators.validate_decision_dict() without modification.
        """
        original_ts = pd.Timestamp("2026-08-21T04:00:00", tz="UTC")

        decision = {
            "decision": "APPROVED",
            "signal": "BUY",
            "position_size": 10,
            "stop_loss": 300.0,
            "reason": "All risk checks passed",
            "timestamp": original_ts,
            "symbol": "AAPL",
        }

        state = StateData(
            positions=[],
            last_acted_bar="2026-08-20T04:00:00Z",
            pending_decision=decision,
            pending_decision_bar_timestamp="2026-08-21T04:00:00Z",
        )

        state_path = str(tmp_path / "state.json")
        save_state(state, state_path)
        loaded = load_state(state_path)

        # The loaded timestamp must be a pd.Timestamp (not a plain string).
        loaded_ts = loaded.pending_decision["timestamp"]
        assert isinstance(loaded_ts, pd.Timestamp), (
            f"Expected pd.Timestamp, got {type(loaded_ts).__name__}"
        )

        # It must be timezone-aware.
        assert loaded_ts.tzinfo is not None, "Loaded timestamp must be timezone-aware"

        # It must represent the same instant in time.
        assert loaded_ts == original_ts

    def test_roundtrip_with_no_pending_decision(self, tmp_path):
        """
        save_state/load_state should still work fine when pending_decision
        is None (the normal non-deferred case).
        """
        state = StateData(
            positions=[],
            last_acted_bar="2026-08-20T04:00:00Z",
            pending_decision=None,
            pending_decision_bar_timestamp=None,
        )

        state_path = str(tmp_path / "state.json")
        save_state(state, state_path)
        loaded = load_state(state_path)

        assert loaded.pending_decision is None

    def test_roundtrip_with_datetime_timestamp(self, tmp_path):
        """
        _json_default also handles plain datetime objects (not just
        pd.Timestamp), in case upstream code ever produces one.
        """
        from datetime import datetime, timezone

        dt_ts = datetime(2026, 8, 21, 4, 0, 0, tzinfo=timezone.utc)

        decision = {
            "decision": "APPROVED",
            "signal": "SELL",
            "position_size": 3,
            "stop_loss": 320.0,
            "reason": "All risk checks passed",
            "timestamp": dt_ts,
            "symbol": "AAPL",
        }

        state = StateData(
            positions=[],
            pending_decision=decision,
        )

        state_path = str(tmp_path / "state.json")
        save_state(state, state_path)
        loaded = load_state(state_path)

        # After round-trip, it comes back as a pd.Timestamp (from the ISO string).
        loaded_ts = loaded.pending_decision["timestamp"]
        assert isinstance(loaded_ts, pd.Timestamp)
        assert loaded_ts.tzinfo is not None

    def test_loaded_decision_passes_execution_validator(self, tmp_path):
        """
        The round-tripped pending_decision must pass execution module's
        validate_decision_dict() without raising — confirming the deferred
        execution path will work end-to-end.
        """
        from execution.validators import validate_decision_dict

        decision = {
            "decision": "APPROVED",
            "signal": "BUY",
            "position_size": 5,
            "stop_loss": 295.0,
            "reason": "All risk checks passed",
            "timestamp": pd.Timestamp("2026-08-21T04:00:00", tz="UTC"),
            "symbol": "AAPL",
        }

        state = StateData(
            positions=[],
            pending_decision=decision,
        )

        state_path = str(tmp_path / "state.json")
        save_state(state, state_path)
        loaded = load_state(state_path)

        # This would raise ValueError if timestamp wasn't reconverted properly.
        validate_decision_dict(loaded.pending_decision)
