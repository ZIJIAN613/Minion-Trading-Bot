# execution/__init__.py
#
# Package initializer for the execution module — the ONLY module in the
# Minions pipeline permitted to place, modify, or cancel orders.
#
# This file makes the execution/ directory a Python package (similar to
# how a namespace works in C++).
#
# The imports below re-export the module's public API so that callers
# can write "from execution import load_config" instead of the longer
# "from execution.config_loader import load_config".  This is the Python
# equivalent of a C++ header that forward-declares public symbols.

"""
execution/ — Order Execution Module for the Minions paper-trading pipeline.

LIVE TRADING IS FORBIDDEN. This module is restricted to Alpaca paper trading
only. The TradingClient is instantiated with paper=True as a hard-coded
literal. No configuration parameter, environment variable, or code path may
route orders to a live brokerage account.
"""

from execution.config_loader import ExecutionConfig, load_config
from execution.execute import execute_decision

__all__ = ["ExecutionConfig", "load_config", "execute_decision"]
