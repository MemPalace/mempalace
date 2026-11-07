"""Early ingest replies through a real loopback HTTP connection."""

import http.client
import json
import threading
from unittest.mock import Mock

from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway, _GatewayHandler
from commonplace.policy import Policy
from commonplace.workers import WorkerSupervisor


def test_card223_guard_drain_real_socket_revoked_token(tmp_path):
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    owner = db.create_user("owner@example.test", is_admin=True)
    workspace = db.create_workspace("personal", owner, personal=True)
    _, token = db.create_token(
        workspace_ref=workspace,
        user_ref=owner,
        name="revoked",
        client="test",
        policy=Policy.build(
            profile="slim", scopes=["memory:write"], write_wing="inbox", trust="untrusted"
        ),
        actor=owner,
    )
    db.revoke("revoked", actor=owner)
    supervisor = WorkerSupervisor(str(tmp_path))
    gateway = Gateway(db, supervisor)
    route_calls = []
    gateway.add_route("POST", "/api/capture", lambda handler: route_calls.append(True))
    statuses, body_reads = [], []
    finished = threading.Event()

    class ObservedHandler(_GatewayHandler):
        def setup(self):
            super().setup()
            self.rfile = Mock(wraps=self.rfile)

        def _send_json(self, status, payload, extra_headers=None):
            super()._send_json(status, payload, extra_headers)
            statuses.append(status)

        def finish(self):
            try:
                super().finish()
            finally:
                body_reads.extend(
                    call
                    for call in self.rfile.method_calls
                    if call[0] != "readline" and call[0] != "close"
                )
                finished.set()

    server = gateway.build_server("127.0.0.1", 0)
    server.RequestHandlerClass = ObservedHandler
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        raw = json.dumps({"transcript": "x" * (1024 * 1024)}).encode()
        try:
            conn.request("POST", "/api/capture", raw, {"Authorization": "Bearer " + token})
            response = conn.getresponse()
            response.read()
        except (OSError, http.client.HTTPException):
            pass
        # A close during upload can hide the response from http.client. Prove
        # the guard on the real server, independently of client send timing.
        assert finished.wait(10), "gateway did not finish the refusal"
        assert statuses == [401]
        assert body_reads == []
        assert route_calls == []
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        supervisor.stop_all()
        db.close()
