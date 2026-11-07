"""Imported startup recall must survive a worker slower than the socket budget."""

import runpy
import time
from pathlib import Path

from commonplace import session
from mempalace.mcp_server import TOOLS
from tests._commonplace_helpers import functional_worker_transport  # noqa: F401
from tests.test_imported_history_search import world  # noqa: F401


def test_imported_startup_injection_survives_slow_search(world, monkeypatch, request):  # noqa: F811
    functional = runpy.run_path(str(Path(__file__).with_name("test_imported_history_search.py")))[
        "test_imported_search_r07_acl_import_startup_injection"
    ]
    search = TOOLS["mempalace_search"]["handler"]
    calls = []

    def delayed(**kwargs):
        calls.append(kwargs)
        time.sleep(session.MEMORY_TIMEOUT * 2)
        return search(**kwargs)

    monkeypatch.setitem(TOOLS["mempalace_search"], "handler", delayed)
    fixtures = {
        name: request.getfixturevalue(name)
        for name in functional.__code__.co_varnames[: functional.__code__.co_argcount]
    }
    functional(**fixtures)
    assert calls, "Startup injection must exercise the real memory search"
