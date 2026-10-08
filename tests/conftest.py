"""Session-wide guard against real network access during tests.

This project's admin-route tests spin up a real BackgroundScheduler (see
test_admin_routes.py) whose jobs run on their own threads; the production
shutdown path (scheduler.shutdown(wait=False), deliberately non-blocking so a
slow job never holds up a container stop) means a thread can still be
mid-fetch when its test function returns. If that straggler isn't caught
before it reaches the network, it can outlive its own test by a long
margin (through tenacity's full retry/backoff budget) and get swept up by
some *unrelated*, later test's @responses.activate mocking - occasionally
breaking that test in a confusing, hard-to-reproduce way.

Blocking real sockets entirely closes this at the root: every HTTP call this
test suite legitimately makes already goes through `responses` (which
intercepts at the urllib3/requests layer, never touching a real socket) or
FastAPI's TestClient (an in-process ASGI transport - also no real socket).
A stray real call now fails immediately and loudly instead of hanging or
silently corrupting another test.
"""
from __future__ import annotations

import socket

import pytest


class BlockedNetworkAccess(Exception):
    pass


def _blocked_connect(*args, **kwargs):
    raise BlockedNetworkAccess(
        "Real network access is blocked during tests - this call should go through "
        "`responses` (HTTP) or a mocked client, not a real socket."
    )


@pytest.fixture(autouse=True, scope="session")
def _block_real_network_access():
    original_connect = socket.socket.connect
    socket.socket.connect = _blocked_connect
    try:
        yield
    finally:
        socket.socket.connect = original_connect
