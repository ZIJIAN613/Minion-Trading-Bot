"""
tests/test_log_system_properties.py — Property-based tests for log_system/.

Driven by the Hypothesis library (already a project dependency).

Hypothesis overview for C++ developers:
  - @given turns a normal test into a property test (like TEST_P in Google
    Test) that auto-generates many random inputs from "strategies".
  - A "strategy" (st.text, st.dictionaries, ...) is a typed random-value
    generator — analogous to a fuzzer input generator.
  - @settings(max_examples=N) controls how many random examples are tried.

Properties covered so far (added per task as waves land):
  Property 1 — Formatter output is valid structured JSON with mandatory fields
               (event, level, timestamp, module); timestamp ends with 'Z' and
               is a valid ISO 8601 UTC datetime.        (task 1.3)
  Property 2 — Extra fields round-trip through the formatter.  (task 1.3)

Tag format (project convention): "Feature: file-logging, Property N: title"

Run with:  venv\\Scripts\\python.exe -m pytest tests/test_log_system_properties.py -v
"""

import io
import json
import logging
from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

# Modules under test.
from log_system.formatter import _JsonFormatter
from log_system.logger import log_structured


# ===========================================================================
# Generator strategies
# ===========================================================================

# Event names: non-empty text. We keep them printable to stay readable, but
# Hypothesis will still throw awkward characters (quotes, backslashes) at us,
# which is exactly what we want to prove the JSON escaping holds.
_event_strategy = st.text(min_size=1, max_size=40)

# Module/logger names: non-empty, restricted to identifier-ish characters so
# they look like real logger names ("risk_engine", "data_ingestion.rest").
_module_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_."),
    min_size=1,
    max_size=30,
)

# Log levels: the five real levels, as (name, numeric) pairs.
_level_strategy = st.sampled_from([
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARNING", logging.WARNING),
    ("ERROR", logging.ERROR),
    ("CRITICAL", logging.CRITICAL),
])

# Extra-field VALUES: JSON-serialisable scalars. We avoid float NaN/inf because
# json.dumps emits them as bare NaN/Infinity which is not strict-JSON parseable;
# that is orthogonal to what these properties assert.
_json_scalar = st.one_of(
    st.text(max_size=40),
    st.integers(min_value=-10_000, max_value=10_000),
    st.booleans(),
    st.none(),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
)

# Extra-field KEYS: identifier-ish strings. We exclude ONLY the four mandatory
# output keys (event/level/timestamp/module) because the formatter legitimately
# owns those, so a caller "extra" with the same name is expected to be shadowed.
#
# We deliberately DO NOT exclude built-in LogRecord attribute names like
# "filename" or "lineno". A Hypothesis run previously proved that such keys used
# to crash the logging call with KeyError; including them here is the regression
# guard that the namespaced-_fields fix in log_structured() holds.
_reserved = {"event", "level", "timestamp", "module"}
_extra_key = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
).filter(lambda k: k not in _reserved)

# Mix random keys with a sampling of known-dangerous LogRecord attribute names
# to make sure the collision case is actually exercised, not just theoretically
# possible.
_dangerous_key = st.sampled_from(["filename", "lineno", "funcName", "module", "name", "args"])
_extra_key_mixed = st.one_of(_extra_key, _dangerous_key).filter(lambda k: k not in _reserved)

_extra_fields_strategy = st.dictionaries(keys=_extra_key_mixed, values=_json_scalar, max_size=6)


# ===========================================================================
# Helper: run one record through a real logger + _JsonFormatter and capture it.
# ===========================================================================

def _emit_and_capture(logger_name: str, level_num: int, event: str, fields: dict) -> str:
    """
    Build an isolated logger NAMED EXACTLY `logger_name`, wired to an in-memory
    buffer, emit one structured record via log_structured(), and return the raw
    formatted string.

    The logger name IS the value that appears in the JSON "module" field, so we
    use the caller-provided name verbatim (no prefixing) — that lets the
    Property 1 test assert module == logger_name directly. To keep examples
    isolated despite reusing names across Hypothesis runs, we clear handlers
    first and set propagate=False.
    """
    buffer = io.StringIO()

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # Clear any handlers left over from a previous example reusing this name.
    logger.handlers.clear()

    handler = logging.StreamHandler(buffer)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)

    log_structured(logger, level_num, event, **fields)

    return buffer.getvalue().strip()


# ===========================================================================
# Property 1 — Formatter output is valid structured JSON with mandatory fields
# Validates: Requirements 1.2, 5.1, 5.2, 5.4, 8.1, 8.2
# ===========================================================================

@settings(max_examples=200)
@given(
    event=_event_strategy,
    module_name=_module_strategy,
    level=_level_strategy,
    fields=_extra_fields_strategy,
)
def test_property_1_valid_json_with_mandatory_fields(event, module_name, level, fields):
    """
    Feature: file-logging, Property 1: Formatter output is valid structured JSON
    with mandatory fields.

    For any event/level/module/extra-fields, the output is a single-line valid
    JSON object containing event, level, timestamp, module — where timestamp
    ends with 'Z' and parses as a valid ISO 8601 UTC datetime.
    """
    level_name, level_num = level

    raw = _emit_and_capture(module_name, level_num, event, fields)

    # Single line: no embedded newline (JSON Lines requirement).
    assert "\n" not in raw

    # Valid JSON.
    obj = json.loads(raw)

    # Mandatory fields present.
    assert obj["event"] == event
    assert obj["level"] == level_name
    assert obj["module"] == module_name
    assert "timestamp" in obj

    # Timestamp ends with 'Z' and is ISO-8601 parseable.
    ts = obj["timestamp"]
    assert ts.endswith("Z"), ts
    # datetime.fromisoformat accepts the offset form; swap trailing 'Z' for
    # '+00:00' (Python < 3.11 does not accept a bare 'Z'). This proves the
    # timestamp is a real UTC datetime, not just a 'Z'-suffixed string.
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0


# ===========================================================================
# Property 2 — Extra fields round-trip through the formatter
# Validates: Requirements 5.5
# ===========================================================================

@settings(max_examples=200)
@given(
    event=_event_strategy,
    module_name=_module_strategy,
    fields=_extra_fields_strategy,
)
def test_property_2_extra_fields_round_trip(event, module_name, fields):
    """
    Feature: file-logging, Property 2: Extra fields round-trip through the
    formatter.

    Every key/value pair passed to log_structured() appears in the parsed JSON
    output with its original value.
    """
    raw = _emit_and_capture(module_name, logging.INFO, event, fields)
    obj = json.loads(raw)

    for key, value in fields.items():
        assert key in obj, f"missing extra field {key!r}"
        # Float values pass through json.dumps/json.loads; compare with a small
        # tolerance to absorb 32-bit float round-off. Non-floats compare exactly.
        if isinstance(value, float):
            assert abs(obj[key] - value) <= 1e-3 * (1 + abs(value))
        else:
            assert obj[key] == value
