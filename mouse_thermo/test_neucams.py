"""Tests for the neucams UDP trigger client."""
from __future__ import annotations

import socket

from .config import NeucamsConfig
from .neucams import NeucamsClient


def _listener() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))          # ephemeral port
    s.settimeout(1.0)
    return s


def _recv(s: socket.socket) -> str:
    return s.recvfrom(1024)[0].decode("utf-8")


def test_begin_recording_sends_folder_then_start():
    srv = _listener()
    port = srv.getsockname()[1]
    c = NeucamsClient(NeucamsConfig(enabled=True, host="127.0.0.1", port=port))
    c.begin_recording("260716_CA001_1")
    assert _recv(srv) == "folder=260716_CA001_1"   # run name set first
    assert _recv(srv) == "start"                   # then acquisition starts
    c.close()
    srv.close()


def test_stop_sends_stop():
    srv = _listener()
    port = srv.getsockname()[1]
    c = NeucamsClient(NeucamsConfig(enabled=True, host="127.0.0.1", port=port))
    c.stop()
    assert _recv(srv) == "stop"
    c.close()
    srv.close()


def test_disabled_client_sends_nothing():
    srv = _listener()
    port = srv.getsockname()[1]
    c = NeucamsClient(NeucamsConfig(enabled=False, host="127.0.0.1", port=port))
    c.begin_recording("run")
    c.stop()
    srv.settimeout(0.2)
    got = None
    try:
        got = _recv(srv)
    except socket.timeout:
        pass
    assert got is None, f"disabled client should send nothing, got {got!r}"
    srv.close()


def test_send_failure_does_not_raise():
    # Point at a closed/unreachable port; UDP send should not raise, and a
    # failed send must never propagate into the caller (the recording).
    c = NeucamsClient(NeucamsConfig(enabled=True, host="127.0.0.1", port=9))
    c.begin_recording("run")   # must not raise
    c.stop()                   # must not raise
    c.close()
