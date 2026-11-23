# orchestrator/__init__.py
#
# Package initializer for the orchestrator module — the top-level loop that
# coordinates all four pipeline stages in a continuous trading bot.
#
# This file makes the orchestrator/ directory a Python package (similar to
# how a namespace works in C++).
#
# The imports below re-export the module's public API so that callers
# can write "from orchestrator import load_config" instead of the longer
# "from orchestrator.config_loader import load_config".

"""
orchestrator/ — Pipeline Orchestrator for the Minions paper-trading bot.

LIVE TRADING IS FORBIDDEN. This module instantiates the Alpaca TradingClient
with paper=True as a HARD-CODED LITERAL. No configuration parameter,
environment variable, or code path may route to a live trading account.
"""

from orchestrator.config_loader import OrchestratorConfig, load_config

# run_loop is not yet fully implemented (will be done in Tasks 7.1 and 7.2),
# but the function stub exists in loop.py.  We use a lazy import so that
# importing the orchestrator package does not fail if run_loop's dependencies
# (strategy, risk_engine, execution module configs) are not yet complete.


def __getattr__(name: str):
    """
    Lazy import for run_loop — only loaded when actually accessed.

    This is Python's module-level __getattr__ hook (PEP 562).  It is called
    when an attribute lookup on this module fails the normal path.  We use it
    to defer the import of run_loop until it's actually called, which avoids
    import errors during incremental development.
    """
    if name == "run_loop":
        from orchestrator.loop import run_loop
        return run_loop
    raise AttributeError(f"module 'orchestrator' has no attribute {name!r}")


__all__ = ["OrchestratorConfig", "load_config", "run_loop"]
