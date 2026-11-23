# strategy/__init__.py
#
# Package initializer for the strategy module.
# This file makes the strategy/ directory a Python package (similar to
# how a namespace works in C++).
#
# The imports below re-export the module's public API so that callers
# can write "from strategy import compute_signal" instead of the longer
# "from strategy.compute import compute_signal".  This is the Python
# equivalent of a C++ header that forward-declares public symbols.

from strategy.config_loader import StrategyConfig, load_config
from strategy.compute import compute_signal
from strategy.validators import validate_bar_dataframe

# __all__ tells Python which names are public when someone does
# "from strategy import *".  It's a convention, not enforced at runtime,
# but tools like linters and IDEs rely on it.
__all__ = ["StrategyConfig", "load_config", "compute_signal", "validate_bar_dataframe"]
