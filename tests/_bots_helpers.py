"""Bots fixture: the console hub (owner, member, another workspace, agent tokens) over real HTTP."""

import pytest

from commonplace.bots import store
from tests._commonplace_helpers import Hub, InProcessLauncher
from tests._console_helpers import ConsoleHub
from tests._mcp_server_helpers import _patch_mcp_server
from tests.test_commonplace_boards import service


class BotsHub(ConsoleHub):
    def bots(self, auth):
        status, body, _ = self.get(auth, "/api/bots")
        assert status == 200, (status, body)
        return body

    def by_board(self, auth):
        return {b["board"] or b["kind"]: b for b in self.bots(auth)["bots"]}

    def add_board(self, slug, name=None):
        service(self.hub, self.person(self.hub.admin)).board_add(
            slug=slug, name=name, wing="shared"
        )

    def archive_board(self, slug):
        boards = service(self.hub, self.person(self.hub.admin))
        with boards.store.connection() as conn:
            conn.execute("UPDATE boards SET archived=1 WHERE slug=?", (slug,))

    def store(self, workspace=None):
        return store.open_bots(self.hub.gateway, workspace or self.hub.workspace)


@pytest.fixture(name="bots_hub")
def bots_hub_fixture(tmp_path, monkeypatch, config, kg):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    _patch_mcp_server(monkeypatch, config, kg)
    hub = Hub(tmp_path / "data", InProcessLauncher(monkeypatch))
    try:
        yield BotsHub(hub)
    finally:
        hub.close()
