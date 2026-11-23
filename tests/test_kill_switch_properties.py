"""
tests/test_kill_switch_properties.py — Property-based test for kill switch
file content independence.

# Feature: kill-switch-alerting, Property 1: Kill switch triggers regardless
# of file content

Uses Hypothesis to verify that _check_kill_switch() returns True for ANY file
content — empty bytes, random UTF-8 text, binary data of arbitrary length —
and returns False when the file does not exist.

Validates: Requirements 1.4

Run with:  python -m pytest tests/test_kill_switch_properties.py -v
"""

import os
import tempfile

import pytest

from hypothesis import given, settings
from hypothesis import strategies as st

from orchestrator.loop import _check_kill_switch


# ===========================================================================
# Hypothesis strategies
# ===========================================================================

# Strategy: arbitrary binary data from 0 to 10,000 bytes.
# st.binary(min_size, max_size) generates random bytes objects — equivalent
# to filling a std::vector<uint8_t> with random values in C++.
arbitrary_binary_content = st.binary(min_size=0, max_size=10000)

# Strategy: random UTF-8 text from 0 to 10,000 characters.
# st.text() generates valid Unicode strings (which Python encodes as UTF-8
# when written to files with encoding="utf-8").
arbitrary_text_content = st.text(min_size=0, max_size=10000)


# ===========================================================================
# Property 1: Kill switch triggers regardless of file content
# ===========================================================================
# Feature: kill-switch-alerting, Property 1: Kill switch triggers regardless
# of file content


class TestKillSwitchFileContentIndependence:
    """
    Property 1: For any file content (empty bytes, random UTF-8 text, binary
    data of any length from 0 to 10,000 bytes), if a file exists at the
    configured kill_switch_file_path, then _check_kill_switch() SHALL return
    True — the decision is based solely on file existence, never on content.
    """

    @given(content=arbitrary_binary_content)
    @settings(max_examples=200)
    def test_any_binary_content_triggers_kill_switch(self, content):
        """
        A file with arbitrary binary content triggers the kill switch.
        """
        # Write the random binary content to a temp file.
        # tempfile.NamedTemporaryFile with delete=False gives us a path that
        # persists after closing — we clean up manually in the finally block.
        fd, path = tempfile.mkstemp()
        try:
            # os.write(fd, bytes) writes raw bytes to the file descriptor.
            os.write(fd, content)
            os.close(fd)

            # The file exists with `content` bytes — must return True.
            assert _check_kill_switch(path) is True
        finally:
            os.unlink(path)

    @given(content=arbitrary_text_content)
    @settings(max_examples=100)
    def test_any_text_content_triggers_kill_switch(self, content):
        """
        A file with arbitrary UTF-8 text content triggers the kill switch.
        """
        fd, path = tempfile.mkstemp()
        try:
            # Encode the text to UTF-8 bytes before writing via the fd.
            os.write(fd, content.encode("utf-8"))
            os.close(fd)

            assert _check_kill_switch(path) is True
        finally:
            os.unlink(path)

    def test_empty_file_triggers_kill_switch(self):
        """
        An empty file (zero bytes) triggers the kill switch — content is
        irrelevant, only existence matters.
        """
        fd, path = tempfile.mkstemp()
        try:
            os.close(fd)  # Close without writing anything — 0-byte file.
            assert _check_kill_switch(path) is True
        finally:
            os.unlink(path)

    def test_nonexistent_path_returns_false(self):
        """
        When the file does not exist, _check_kill_switch returns False.
        """
        # Use a path that definitely doesn't exist.
        path = os.path.join(tempfile.gettempdir(), "nonexistent_kill_switch_xyz_12345")
        # Ensure it really doesn't exist (defensive).
        if os.path.exists(path):
            os.unlink(path)

        assert _check_kill_switch(path) is False
