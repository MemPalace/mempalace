"""Project panel: strict format, authorized writes, and a project without one left unchanged."""

import json

import pytest

from commonplace.projects import panel
from tests._projects_helpers import projects_hub_fixture  # noqa: F401

PANEL = {
    "format": "commonplace.panel/1",
    "title": "Atlas",
    "highlights": ["Beta ships Friday", "3 open risks"],
    "links": [
        {"label": "Runbook", "url": "https://example.com/runbook"},
        {"label": "Status", "url": "http://status.example.com:8080/a?b=1#c"},
    ],
}
PATH = "/api/projects/work/panel"


def put(h, auth, doc, slug="work"):
    return h.request(auth, "PUT", f"/api/projects/{slug}/panel", {"panel": doc})[:2]


def stored(h, slug="work"):
    return h.get(h.owner, f"/api/projects/{slug}/panel")[1]["panel"]


def agent(h, name, **kwargs):
    return h.hub.token(name, profile="full", **kwargs)


# The format and its limits, without a server


def test_valid_document_is_kept_as_given():
    assert panel.validate(PANEL) == PANEL
    assert panel.validate({"format": panel.FORMAT, "highlights": ["  one  "]})["highlights"] == [
        "one"
    ]


def test_parse_reads_a_repo_file():
    assert panel.parse(json.dumps(PANEL)) == PANEL
    assert panel.parse(json.dumps(PANEL).encode()) == PANEL
    with pytest.raises(panel.PanelInvalid, match="not valid JSON"):
        panel.parse("{nope")
    with pytest.raises(panel.PanelInvalid, match="not valid JSON"):
        panel.parse(b"\xff\xfe")
    with pytest.raises(panel.PanelTooLarge):
        panel.parse(" " * (panel.MAX_BYTES * 2 + 1))


def mutate(**changes):
    return {**PANEL, **changes}


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ([], "must be a JSON object"),
        ({}, "format must be"),
        (mutate(format="commonplace.panel/2"), "format must be"),
        (mutate(extra=1), "unknown field: extra"),
        (mutate(title=5), "title must be text"),
        (mutate(title=" "), "title must not be empty"),
        (mutate(title="x" * 61), "title is longer than 60"),
        (mutate(highlights="one"), "highlights must be a list"),
        (mutate(highlights=["a"] * 7), "at most 6 highlights"),
        (mutate(highlights=["x" * 201]), "highlight 1 is longer than 200"),
        (mutate(highlights=[3]), "highlight 1 must be text"),
        (mutate(highlights=["ok", ""]), "highlight 2 must not be empty"),
        (mutate(highlights=["<script>alert(1)</script>"]), "HTML and script are not allowed"),
        (mutate(highlights=["a <b>bold</b> claim"]), "HTML and script are not allowed"),
        (mutate(highlights=["<!-- hidden -->"]), "HTML and script are not allowed"),
        (mutate(highlights=["two\nlines"]), "control characters"),
        (mutate(highlights=["bidi ‮override"]), "control characters"),
        (mutate(links="https://example.com"), "links must be a list"),
        (mutate(links=[{"label": "a", "url": "https://e.com"}] * 7), "at most 6 links"),
        (mutate(links=["https://example.com"]), 'link 1 must have exactly "label" and "url"'),
        (mutate(links=[{"label": "a"}]), 'link 1 must have exactly "label" and "url"'),
        (
            mutate(links=[{"label": "a", "url": "https://e.com", "rel": "x"}]),
            'link 1 must have exactly "label" and "url"',
        ),
        (mutate(links=[{"label": "x" * 41, "url": "https://e.com"}]), "label is longer than 40"),
        (mutate(links=[{"label": "<b>x</b>", "url": "https://e.com"}]), "plain text"),
        (mutate(links=[{"label": "javascript:alert(1)", "url": "https://e.com"}]), "plain text"),
        ({"format": panel.FORMAT, "highlights": [], "links": []}, "at least one highlight"),
    ],
)
def test_invalid_documents_say_what_to_change(doc, message):
    with pytest.raises(panel.PanelInvalid, match=message):
        panel.validate(doc)


def test_oversized_document_is_refused_by_size():
    big = {
        "format": panel.FORMAT,
        "title": "t" * 60,
        "highlights": ["h" * 200] * 6,
        "links": [{"label": "l" * 40, "url": "https://example.com/" + "p" * 470}] * 6,
    }
    with pytest.raises(panel.PanelTooLarge, match="larger than 4096 bytes"):
        panel.validate(big)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<b>x</b>",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "mailto:a@example.com",
        "//example.com/x",
        "example.com/x",
        "/relative",
        "https://",
        "https:///path",
        "https://user:pass@example.com/",
        "https://good.example.com@evil.example.net/",
        "https://example.com/a b",
        "https://example.com/\tx",
        " https://example.com/",
        "https://example.com/‮x",
        "https://example.com:99999/",
        'https://example.com/"onmouseover="x',
        "https://example.com/<x>",
        "https://example.com\\@evil.example.net/",
        "https://example.com/" + "a" * 500,
        None,
        5,
    ],
)
def test_unsafe_links_are_refused(url):
    with pytest.raises(panel.PanelInvalid):
        panel.validate(mutate(links=[{"label": "Go", "url": url}]))


def test_http_and_https_links_pass_in_any_case():
    for url in ("HTTP://Example.com/x", "https://example.com:8443/a?b=c#d", "https://[::1]/x"):
        assert panel.validate(mutate(links=[{"label": "Go", "url": url}]))["links"][0]["url"] == url


# The routes, what is shown


def test_valid_panel_is_stored_and_read_by_anyone_who_can_read_the_project(projects_hub):
    h = projects_hub
    assert h.request(h.owner, "GET", PATH)[1] == {"panel": None}
    status, body = put(h, h.owner, PANEL)
    assert status == 200
    assert body["panel"] == PANEL and body["updated_at"]
    for reader in (h.owner, h.member, h.a1):
        status, body, _ = h.request(reader, "GET", PATH)
        assert status == 200 and body["panel"] == PANEL


def test_agent_submits_and_resubmitting_replaces(projects_hub):
    h = projects_hub
    status, body = put(h, h.a1, PANEL)
    assert status == 200 and body["panel"] == PANEL
    newer = {"format": panel.FORMAT, "highlights": ["Only this"]}
    assert put(h, h.a1, newer)[1]["panel"] == {**newer, "links": []}
    assert stored(h) == {**newer, "links": []}


def test_clearing_removes_the_panel(projects_hub):
    h = projects_hub
    put(h, h.owner, PANEL)
    assert put(h, h.owner, None) == (200, {"panel": None})
    assert stored(h) is None
    assert put(h, h.owner, None) == (200, {"panel": None})


def test_a_project_without_a_panel_is_unchanged(projects_hub):
    h = projects_hub
    assert h.create_board(h.owner, "alpha")[0] == 201
    before = {slug: h.project(h.owner, slug) for slug in ("work", "alpha")}
    boards = h.get(h.owner, "/api/boards")[1]
    assert put(h, h.owner, PANEL, "work")[0] == 200
    assert h.project(h.owner, "alpha") == before["alpha"]
    assert h.project(h.owner, "work") == before["work"]
    assert "panel" not in before["alpha"][1]
    assert stored(h, "alpha") is None
    assert h.get(h.owner, "/api/boards")[1] == boards


def test_panels_are_per_project(projects_hub):
    h = projects_hub
    h.create_board(h.owner, "alpha")
    put(h, h.owner, PANEL, "work")
    other = {"format": panel.FORMAT, "highlights": ["Alpha only"]}
    put(h, h.owner, other, "alpha")
    assert stored(h, "work") == PANEL
    assert stored(h, "alpha")["highlights"] == ["Alpha only"]


# The routes, what is refused


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"panel": {"format": "x"}}, "format must be"),
        (
            {"panel": {"format": panel.FORMAT, "highlights": ["<img src=x onerror=1>"]}},
            "plain text",
        ),
        (
            {"panel": {"format": panel.FORMAT, "links": [{"label": "a", "url": "javascript:1"}]}},
            "http://",
        ),
        ({"panel": "text"}, "must be a JSON object"),
        ({"format": panel.FORMAT, "highlights": ["x"]}, 'exactly {"panel"'),
        ({"panel": PANEL, "other": 1}, 'exactly {"panel"'),
    ],
)
def test_invalid_panel_is_refused_with_a_clear_message_and_nothing_stored(
    projects_hub, body, message
):
    h = projects_hub
    put(h, h.owner, PANEL)
    status, response, _ = h.request(h.owner, "PUT", PATH, body)
    assert status == 400
    assert response["code"] == "invalid_panel" and message in response["error"]
    assert stored(h) == PANEL


def test_oversized_panel_is_refused(projects_hub):
    h = projects_hub
    big = {
        "format": panel.FORMAT,
        "title": "t" * 60,
        "highlights": ["h" * 200] * 6,
        "links": [{"label": "l" * 40, "url": "https://example.com/" + "p" * 470}] * 6,
    }
    status, response = put(h, h.owner, big)
    assert status == 413
    assert response["code"] == "panel_too_large" and "4096 bytes" in response["error"]
    assert stored(h) is None
    huge = {"panel": {"format": panel.FORMAT, "highlights": ["x" * 100_000]}}
    status, response, _ = h.request(h.owner, "PUT", PATH, huge)
    assert status == 400 and "longer than 200" in response["error"]
    assert stored(h) is None


@pytest.mark.parametrize(
    "scheme", ["javascript:alert(1)", "data:text/html,x", "file:///x", "ftp://e.com"]
)
def test_unsafe_link_scheme_is_refused_over_the_route(projects_hub, scheme):
    h = projects_hub
    status, response = put(
        h, h.owner, {"format": panel.FORMAT, "links": [{"label": "Go", "url": scheme}]}
    )
    assert status == 400 and "http://" in response["error"]
    assert stored(h) is None


def test_only_someone_who_may_write_the_project_sets_it(projects_hub):
    h = projects_hub
    put(h, h.owner, PANEL)
    newer = {"format": panel.FORMAT, "highlights": ["Changed"]}
    reader = agent(h, "reader", scopes=["memory:read"], read_wings=["work"])
    no_write_wing = agent(
        h,
        "elsewhere",
        scopes=["memory:read", "memory:write"],
        read_wings=["work"],
        write_wings=["zz"],
        trust="trusted",
    )
    add_only = h.untrusted("work")
    for token in (reader, no_write_wing, add_only):
        status, body = put(h, token, newer)
        assert status == 403 and body["code"] == "not_allowed", token
        assert put(h, token, None)[0] == 403
        assert stored(h) == PANEL
        assert h.request(token, "GET", PATH)[0] == 200 or token == add_only


def test_anonymous_and_other_workspace_are_refused(projects_hub):
    h = projects_hub
    put(h, h.owner, PANEL)
    assert h.request(None, "PUT", PATH, {"panel": PANEL})[0] == 401
    assert h.request(None, "GET", PATH)[0] == 401
    for call in (
        lambda: h.request(h.other, "GET", PATH),
        lambda: h.request(h.other, "PUT", PATH, {"panel": PANEL}),
    ):
        assert call()[0] in (403, 404)
    assert stored(h) == PANEL


def test_unknown_project_is_not_found(projects_hub):
    h = projects_hub
    assert put(h, h.owner, PANEL, "nope")[0] == 404
    assert h.request(h.owner, "GET", "/api/projects/nope/panel")[0] == 404


def test_a_member_with_board_write_authority_may_set_it(projects_hub):
    h = projects_hub
    assert put(h, h.member, PANEL)[0] == 200
    assert stored(h) == PANEL
