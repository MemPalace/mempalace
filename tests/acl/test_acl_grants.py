"""Grants file loader: every refusal the server relies on."""

import os
import sys

import pytest
from _acl_helpers import key, sha, write_grants

from mempalace.acl import Identity
from mempalace.acl.grants import GrantsError, TokenResolver, load_for_server, load_grants

OK = [key("alice", "t-alice", wings=["Alpha-Team"], read_wings=["beta"])]


def test_valid_file_normalizes_wings(tmp_path):
    store = load_grants(write_grants(tmp_path, OK))
    scope = store.scope_for(Identity("alice", "grants"))
    assert scope.kind == "locked"
    assert scope.write == {"alpha_team"}
    assert scope.read == {"alpha_team", "beta"}


def test_yaml_is_accepted(tmp_path):
    path = tmp_path / "g.yaml"
    path.write_text(f"keys:\n  - name: bob\n    token_sha256: {sha('b')}\n    wings: [gamma]\n")
    os.chmod(path, 0o600)
    assert load_grants(str(path)).keys[0].wings == {"gamma"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_loose_mode_refused(tmp_path, mode):
    with pytest.raises(GrantsError, match="too open"):
        load_grants(write_grants(tmp_path, OK, mode=mode))


@pytest.mark.parametrize(
    "keys, top, message",
    [
        ([key("a", "1"), key("b", "1")], {}, "duplicate"),
        ([key("a", "1"), key("a", "2")], {}, "duplicate"),
        ([key("a", "1", wings=["My-Proj", "my_proj"])], {}, "same wing"),
        ([key("a", "1", wings=["x"], read_wings=["X"])], {}, "both"),
        ([key("a", "1", surprise=1)], {}, "unknown key"),
        ([key("a", "1", wings="alpha")], {}, "list of wing"),
        ([{"name": "a", "token_sha256": "abc"}], {}, "64 lowercase hex"),
        ([{"name": "a", "token": "plain"}], {}, "unknown key"),
        ([key("a", "1")], {"bogus": 1}, "unknown key"),
        ([key("a", "1")], {"default_wing_mode": "wide"}, "default_wing_mode"),
        ([key("a", "1", clearance="ghost")], {"levels": ["public"]}, "not in levels"),
        ([key("a", "1")], {"wings": {"A-b": {}, "a_b": {}}}, "duplicate wing"),
    ],
)
def test_invalid_files(tmp_path, keys, top, message):
    with pytest.raises(GrantsError, match=message):
        load_grants(write_grants(tmp_path, keys, **top))


def test_empty_and_garbage_files(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("")
    os.chmod(path, 0o600)
    with pytest.raises(GrantsError, match="empty"):
        load_grants(str(path))
    path.write_text("{{{ not: [valid")
    with pytest.raises(GrantsError, match="not a valid grants file"):
        load_grants(str(path))
    with pytest.raises(GrantsError, match="cannot read"):
        load_grants(str(tmp_path / "missing.json"))


def test_hash_equal_to_legacy_token_refused(tmp_path):
    path = write_grants(tmp_path, [key("a", "same-token")])
    with pytest.raises(GrantsError, match="server token"):
        load_grants(path, legacy_token="same-token")


@pytest.mark.parametrize(
    "keys, top, setting",
    [
        ([key("a", "1", clearance="staff")], {"levels": ["staff"]}, "clearance"),
        ([key("a", "1", compartments=["board"])], {}, "compartments"),
        ([key("a", "1")], {"default_wing_mode": "open"}, "default_wing_mode: open"),
        ([key("a", "1")], {"wings": {"w": {"mode": "open"}}}, "mode: open"),
    ],
)
def test_reserved_keys_parse_but_server_refuses(tmp_path, keys, top, setting):
    path = write_grants(tmp_path, keys, **top)
    assert load_grants(path).unenforced()  # parsed and validated
    with pytest.raises(GrantsError, match="not enforced yet") as exc:
        load_for_server(path, "legacy")
    assert setting in str(exc.value)


def test_locked_mode_and_levels_alone_are_fine(tmp_path):
    path = write_grants(
        tmp_path, OK, levels=["public", "staff"], default_wing_mode="locked",
        wings={"alpha_team": {"mode": "locked"}},
    )  # fmt: skip
    assert load_for_server(path, "legacy").unenforced() == []


def test_server_needs_a_legacy_token_and_not_tokenless(tmp_path):
    path = write_grants(tmp_path, OK)
    with pytest.raises(GrantsError, match="token"):
        load_for_server(path, "")
    with pytest.raises(GrantsError, match="local_only_tokenless"):
        load_for_server(path, "legacy", tokenless=True)


def test_resolver_unknown_revoked_legacy(tmp_path):
    keys = [key("alice", "t-alice", wings=["a"]), key("old", "t-old", revoked=True)]
    resolver = TokenResolver(load_grants(write_grants(tmp_path, keys)), "legacy")

    def who(auth):
        found = resolver.resolve({"Authorization": auth} if auth is not None else {})
        return found and (found.subject, found.source)

    assert who("Bearer t-alice") == ("alice", "grants")
    assert who("Bearer legacy") == ("legacy", "legacy")
    assert who("Bearer t-old") is None
    assert who("Bearer nope") is None
    assert who("Bearer ") is None
    assert who("Basic t-alice") is None
    assert who("t-alice") is None
    assert who(None) is None
    assert who("Bearer caf\xe9") is None  # non-ASCII must not raise


def test_empty_token_hash_is_refused_and_never_resolves(tmp_path):
    empty = {"name": "ghost", "token_sha256": sha(""), "wings": ["alpha"]}
    with pytest.raises(GrantsError, match="empty"):
        load_grants(write_grants(tmp_path, [empty]))
    # defence in depth: even a store built around the empty hash gives no identity
    from mempalace.acl.grants import GrantStore, Key

    resolver = TokenResolver(GrantStore([Key("ghost", sha(""))]), "")
    for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer "}):
        assert resolver.resolve(headers) is None
    assert resolver.resolve({"Authorization": "Bearer    "}) is None
    assert resolver.resolve({"Authorization": "Bearer \t"}) is None
    assert TokenResolver(GrantStore([]), "").resolve({"Authorization": "Bearer "}) is None


def test_deeply_nested_file_is_a_clean_grants_error(tmp_path):
    for text in ("[" * 100000, '{"keys": ' * 50000):
        path = tmp_path / "deep.json"
        path.write_text(text)
        os.chmod(path, 0o600)
        with pytest.raises(GrantsError):
            load_grants(str(path))


def test_duplicate_keys_in_json_and_yaml_are_refused(tmp_path):
    for name, text in (
        ("d.json", '{"keys": [], "keys": []}'),
        ("d.yaml", "keys: []\nkeys: []\n"),
        (
            "n.yaml",
            f"keys:\n  - name: a\n    token_sha256: {sha('a')}\n    wings: [x]\n    wings: [y]\n",
        ),
    ):
        path = tmp_path / name
        path.write_text(text)
        os.chmod(path, 0o600)
        with pytest.raises(GrantsError, match="duplicate key"):
            load_grants(str(path))


@pytest.mark.parametrize("value", ["false", "no", "yes", 0, 1, None, []])
def test_revoked_must_be_a_real_boolean(tmp_path, value):
    with pytest.raises(GrantsError, match="revoked"):
        load_grants(write_grants(tmp_path, [key("a", "1", revoked=value)]))


def test_empty_key_list_warns_loudly(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        load_grants(write_grants(tmp_path, []))
    assert any("no keys" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def _yaml(tmp_path, text, name="g.yaml"):
    path = tmp_path / name
    path.write_text(text)
    os.chmod(path, 0o600)
    return str(path)


def test_yaml_anchors_and_merge_keys_still_work(tmp_path):
    text = f"""
keys:
  - &base
    name: alice
    token_sha256: {sha("a")}
    wings: &w [alpha]
  - <<: *base
    name: bob
    token_sha256: {sha("b")}
  - name: carol
    token_sha256: {sha("c")}
    wings: *w
"""
    store = load_grants(_yaml(tmp_path, text))
    by_name = {k.name: k for k in store.keys}
    assert by_name["bob"].wings == by_name["carol"].wings == {"alpha"}  # merged and anchored
    assert by_name["bob"].token_sha256 == sha("b")  # the explicit key overrides the merge


def test_duplicate_key_next_to_a_merge_is_still_refused(tmp_path):
    text = f"""
keys:
  - &base {{name: a, token_sha256: "{sha("a")}"}}
  - <<: *base
    name: b
    name: c
    token_sha256: "{sha("b")}"
"""
    with pytest.raises(GrantsError, match="duplicate key"):
        load_grants(_yaml(tmp_path, text))


@pytest.mark.parametrize(
    "auth",
    [
        "Bearer t1 ",
        "Bearer  t1",
        "Bearer \tt1",
        "Bearer t1\t",
        "Bearer t1\n",
        " Bearer t1",
        "bearer t1",
        "BEARER t1",
        "Bearer: t1",
    ],
)
def test_bearer_parsing_is_strict_for_grants_and_legacy(tmp_path, auth):
    resolver = TokenResolver(
        load_grants(write_grants(tmp_path, [key("a", "t1", wings=["x"])])), "L1"
    )
    assert resolver.resolve({"Authorization": auth}) is None
    assert resolver.resolve({"Authorization": auth.replace("t1", "L1")}) is None
    assert resolver.resolve({"Authorization": "Bearer t1"}).subject == "a"
    assert resolver.resolve({"Authorization": "Bearer L1"}).source == "legacy"


def test_key_names_are_normalized_and_collide_case_insensitively(tmp_path):
    for first, second in (("A", "a "), ("bob", " BOB"), ("Eve", "eve")):
        with pytest.raises(GrantsError, match="duplicate"):
            load_grants(write_grants(tmp_path, [key(first, "1"), key(second, "2")]))
    store = load_grants(write_grants(tmp_path, [key("  Bob ", "t", wings=["x"])]))
    assert store.keys[0].name == "bob"
    scope = store.scope_for(TokenResolver(store, "").resolve({"Authorization": "Bearer t"}))
    assert scope.subject == "bob"


def test_scope_for_a_revoked_or_unknown_key_is_denied(tmp_path):
    store = load_grants(write_grants(tmp_path, [key("old", "t", wings=["x"], revoked=True)]))
    assert store.scope_for(Identity("old", "grants")).kind == "denied"
    assert store.scope_for(Identity("nobody", "grants")).kind == "denied"
    assert store.scope_for(Identity("anyone", "legacy")).kind == "admin"


def test_padded_credentials_fail_even_when_the_padded_token_is_the_real_one(tmp_path):
    # the configured token itself carries the padding: only the strict check stops it
    store = load_grants(
        write_grants(tmp_path, [key("a", "t1 ", wings=["x"]), key("b", " t2", wings=["x"])])
    )
    resolver = TokenResolver(store, "L1 ")
    for auth in ("Bearer t1 ", "Bearer  t2", "Bearer L1 "):
        assert resolver.resolve({"Authorization": auth}) is None


@pytest.mark.parametrize("mode", ["Open", "LOCKED", "junk", "", 1, True, None, ["open"], {"a": 1}])
def test_wing_mode_must_be_exactly_open_or_locked(tmp_path, mode):
    with pytest.raises(GrantsError, match="mode"):
        load_grants(write_grants(tmp_path, [key("a", "1")], wings={"w": {"mode": mode}}))


def test_wing_settings_must_be_a_mapping_with_known_keys(tmp_path):
    for bad in (["w"], {"w": {"colour": "red"}}, {"w": "locked"}, {"": {}}, {"--": {}}):
        with pytest.raises(GrantsError):
            load_grants(write_grants(tmp_path, [key("a", "1")], wings=bad))


def test_duplicate_level_names_are_refused(tmp_path):
    for levels in (["a", "a"], ["public", "staff", "public"], "staff", [1, 2], [["x"]]):
        with pytest.raises(GrantsError, match="levels"):
            load_grants(write_grants(tmp_path, [key("a", "1")], levels=levels))


@pytest.mark.parametrize("name", ["", "  ", "\t\n", 123, None, ["a"], True])
def test_key_names_must_be_non_empty_strings(tmp_path, name):
    entry = {"name": name, "token_sha256": sha("x")}
    with pytest.raises(GrantsError, match="non-empty name"):
        load_grants(write_grants(tmp_path, [entry]))
    with pytest.raises(GrantsError, match="non-empty name"):
        load_grants(write_grants(tmp_path, [{"token_sha256": sha("x")}]))


@pytest.mark.parametrize(
    "digest",
    [
        sha("x").upper(),
        sha("x") + "\n",
        "\n" + sha("x"),
        sha("x")[:-1],
        sha("x") + "0",
        " " + sha("x"),
        "g" * 64,
        "",
        None,
        123,
    ],
)
def test_token_hash_must_be_exactly_64_lowercase_hex(tmp_path, digest):
    with pytest.raises(GrantsError, match="64 lowercase hex"):
        load_grants(write_grants(tmp_path, [{"name": "a", "token_sha256": digest}]))


def test_run_internal_restores_the_previous_scope_inline_and_on_error():
    from mempalace import acl

    locked = acl.Scope("locked", "g", read=frozenset({"a"}), write=frozenset({"a"}))
    inside = []
    with acl.request_scope(locked):
        acl.run_internal(lambda: inside.append(acl.current_scope().kind))()
        assert acl.current_scope() is locked  # restored after a normal return

        def boom():
            inside.append(acl.current_scope().kind)
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            acl.run_internal(boom)()
        assert acl.current_scope() is locked  # restored after an exception
    assert inside == ["internal", "internal"]
    assert acl.run_internal(lambda a, b=2: (a, b))(1, b=3) == (1, 3)  # args pass through


def test_scrub_result_hides_failures_with_or_without_error_text():
    from mempalace import acl

    generic = {"success": False, "error": "Request failed"}
    assert acl.scrub_result({"success": False}) == generic
    assert acl.scrub_result({"success": False, "reason": "/srv/x beta"}) == generic
    assert acl.scrub_result({"error": "/srv/x"}) == generic
    assert acl.scrub_result({"success": True, "drawer_id": "d"}) == {
        "success": True,
        "drawer_id": "d",
    }
    assert acl.scrub_result({"wings": {}}) == {"wings": {}}
    assert acl.scrub_result("text") == "text"
    assert acl.scrub_result({"success": False}) is not acl.GENERIC_ERROR  # a copy


def test_input_problem_covers_diary_write_fields():
    from mempalace import acl

    ok = {"agent_name": "bot", "topic": "general", "entry": "text", "wing": "a"}
    assert acl.input_problem("mempalace_diary_write", ok) is None
    assert "content" in acl.input_problem("mempalace_diary_write", {**ok, "entry": "  "})
    assert "topic" in acl.input_problem("mempalace_diary_write", {**ok, "topic": "../x"})
    assert "agent_name" in acl.input_problem("mempalace_diary_write", {**ok, "agent_name": "../"})
    alias = {"agent_name": "bot", "content": ""}  # `content` stands in for `entry`
    assert "content" in acl.input_problem("mempalace_diary_write", alias)
    assert acl.input_problem("mempalace_diary_write", {}) is None  # missing: handler's job
    assert acl.input_problem("mempalace_diary_write", {**ok, "topic": None}) is None
    assert acl.input_problem("mempalace_get_aaak_spec", {"content": ""}) is None
    assert "room" in acl.input_problem("mempalace_add_drawer", {"room": "../x", "content": "c"})
