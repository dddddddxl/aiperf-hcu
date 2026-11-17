"""Default tests cannot contact non-loopback networks."""
import socket
import os
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def local_network_only(monkeypatch):
    monkeypatch.setenv("HCU_TEST_LOCAL_ONLY", "1")
    guard = str(Path(__file__).parent / "network_guard")
    monkeypatch.setenv("PYTHONPATH", guard + os.pathsep + os.environ.get("PYTHONPATH", ""))
    original = socket.socket.connect
    def checked(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("Tests prohibit external network")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", checked)
