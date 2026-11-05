"""Early ingest replies through a real loopback HTTP connection."""

import http.client
import json
import threading

from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
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
    gateway.add_route("POST", "/api/capture", lambda handler: None)
    server = gateway.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
    status = None
    try:
        raw = json.dumps({"transcript": "x" * (1024 * 1024)}).encode()
        try:
            conn.request("POST", "/api/capture", raw, {"Authorization": "Bearer " + token})
            response = conn.getresponse()
            status = response.status
            response.read()
        except (OSError, http.client.HTTPException):
            pass
        assert status == 401
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        supervisor.stop_all()
        db.close()
