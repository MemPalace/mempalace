"""Force the early-close interleaving in the real-socket guard proof."""

import http.client
import threading
from http.server import ThreadingHTTPServer

import test_commonplace_gateway_drain_unit as drain_test


def test_revoked_guard_when_server_closes_during_send(tmp_path, monkeypatch):
    closed = threading.Event()
    send_failures = []
    shutdown_request = ThreadingHTTPServer.shutdown_request

    def close_request(server, request):
        try:
            shutdown_request(server, request)
        finally:
            closed.set()

    class InterruptedConnection(http.client.HTTPConnection):
        def send(self, data):
            if isinstance(data, bytes) and data.startswith(b'{"transcript":'):
                # Headers went over a real socket. Let the real gateway finish
                # and close before the body send reports a platform-dependent
                # broken pipe; no sleep or socket-buffer-size assumption.
                assert closed.wait(10), "gateway did not reject before body upload"
                send_failures.append(True)
                raise BrokenPipeError("server closed before body upload finished")
            return super().send(data)

    monkeypatch.setattr(ThreadingHTTPServer, "shutdown_request", close_request)
    monkeypatch.setattr(drain_test.http.client, "HTTPConnection", InterruptedConnection)
    drain_test.test_card223_guard_drain_real_socket_revoked_token(tmp_path)
    assert send_failures == [True]
