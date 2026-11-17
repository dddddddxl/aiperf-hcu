"""Injected only by tests, including into the real AIPerf subprocesses."""
import os
import socket

if os.environ.get("HCU_TEST_LOCAL_ONLY") == "1":
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    def check(address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError("External network disabled by HCU test guard")
    def connect(sock, address):
        check(address)
        return original(sock, address)
    def connect_ex(sock, address):
        check(address)
        return original_ex(sock, address)
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
