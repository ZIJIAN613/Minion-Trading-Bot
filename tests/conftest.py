"""
tests/conftest.py — Shared pytest fixtures and safety guards for the suite.

WHY THIS FILE EXISTS
--------------------
A full-suite run was observed making a LIVE Alpaca API call and dispatching a
REAL Telegram message (a "Cycle Skipped" 401 alert). That happens because
alerting/config.yaml ships with real Telegram credentials + telegram_bot_enabled:
true, and some code path reached the real network during tests.

To make the suite deterministic and safe WITHOUT changing any committed config
or application code, this conftest installs two autouse guards:

  1. _block_remote_network — blocks OUTBOUND network egress to real hosts, so an
     accidental live Alpaca/Telegram call fails fast inside the test. It
     deliberately ALLOWS loopback / AF_UNIX / socketpair connections, because
     Python's asyncio event loop on Windows creates an internal self-pipe via
     socket.socketpair() -> connect() to 127.0.0.1, which is NOT a real network
     call and must not be blocked.

  2. _no_live_telegram_bot — prevents alerting.initialize() from starting the
     real TelegramBotListener thread (which polls Telegram's servers). It does
     NOT stub send_alert, so tests that assert on the alert HTTP path (by mocking
     urllib themselves) still see the call.

Tests can opt out of the network guard with @pytest.mark.allow_network.

pytest note for C++ developers:
  - A fixture is a scoped setup/teardown helper; autouse=True runs it for every
    test automatically.
  - monkeypatch is pytest's RAII-style patcher: changes revert at test end.
"""

import socket

import pytest


# Real socket.connect, captured before we patch it, so the wrapper can delegate
# for allowed (loopback) destinations.
_REAL_CONNECT = socket.socket.connect


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: permit real outbound socket connections in this test.",
    )


def _is_loopback_address(address) -> bool:
    """
    Return True if `address` targets the local machine (loopback / self-pipe),
    which we must allow so asyncio's internal socketpair() works on Windows.

    address for AF_INET/AF_INET6 is a (host, port, ...) tuple; for AF_UNIX it is
    a string path. We treat AF_UNIX and any localhost host as loopback.
    """
    # AF_UNIX (string) — always local.
    if isinstance(address, str):
        return True
    # AF_INET / AF_INET6 — inspect the host component.
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
        return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "")
    # Unknown address shape — be conservative and treat as NON-loopback so it
    # gets blocked (safer default for a test guard).
    return False


@pytest.fixture(autouse=True)
def _block_remote_network(monkeypatch, request):
    """
    Block outbound connections to real (non-loopback) hosts. Loopback and
    AF_UNIX connections are delegated to the real connect() so event-loop
    self-pipes and any localhost test servers keep working.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    def _guarded_connect(self, address, *args, **kwargs):
        if _is_loopback_address(address):
            return _REAL_CONNECT(self, address, *args, **kwargs)
        raise RuntimeError(
            "Outbound network connection blocked during tests "
            f"(target={address!r}). If this test truly needs the network, "
            "mark it with @pytest.mark.allow_network."
        )

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect, raising=True)


@pytest.fixture(autouse=True)
def _no_live_telegram_bot(monkeypatch, request):
    """
    Stop alerting.initialize() from starting the real Telegram bot listener
    thread. We patch TelegramBotListener.start to a no-op so no polling thread
    is spawned. send_alert is intentionally left untouched: tests that assert on
    the alert HTTP path mock urllib themselves and still work, while the network
    guard above stops any real Telegram POST from leaving the machine.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    # The telegram bot's OWN unit tests manage the listener lifecycle directly
    # (and mock the network themselves). Do not neutralize start() there, or we
    # would break tests that assert the listener thread was created.
    test_path = str(request.node.fspath)
    if "test_telegram_bot" in test_path:
        return

    try:
        import alerting.telegram_bot as tb
    except Exception:  # pragma: no cover - alerting always importable in this repo
        return

    if hasattr(tb, "TelegramBotListener"):
        monkeypatch.setattr(
            tb.TelegramBotListener, "start", lambda self, *a, **k: None, raising=True
        )
