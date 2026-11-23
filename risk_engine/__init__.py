# risk_engine/__init__.py
#
# Package initializer for the risk_engine module.
# This file makes the risk_engine/ directory a Python package (similar to
# how a namespace works in C++).
#
# The imports below re-export the module's public API so that callers
# can write "from risk_engine import evaluate_signal" instead of the longer
# "from risk_engine.evaluate import evaluate_signal".  This is the Python
# equivalent of a C++ header that forward-declares public symbols.

from risk_engine.config_loader import RiskConfig, load_config
from risk_engine.evaluate import evaluate_signal

# __all__ tells Python which names are public when someone does
# "from risk_engine import *".  It's a convention, not enforced at runtime,
# but tools like linters and IDEs rely on it.
__all__ = ["RiskConfig", "load_config", "evaluate_signal"]
