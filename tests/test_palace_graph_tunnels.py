"""Tests for explicit tunnel helpers in mempalace.palace_graph."""

import logging
import os
import stat
import sys
from unittest.mock import MagicMock, patch

import pytest

with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    import mempalace.palace_graph as palace_graph


def _use_tmp_tunnel_file(monkeypatch, tmp_path):
    """Redirect both the tunnel-file resolver and the legacy-file check at the
    tmp_path so existing tests stay in the configured-path branch and don't
    accidentally trip the new legacy-file warning branch in _load_tunnels.

    Also neutralizes ``_get_collection`` so the endpoint-existence validation
    added for #1468 falls through to the "can't verify, allow" branch by
    default. Tests that exercise the validation path supply their own stub
    via a subsequent monkeypatch.setattr.
    """
    tunnel_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tunnel_file))
    monkeypatch.setattr(
        palace_graph,
        "_legacy_tunnel_file",
        lambda: str(tmp_path / "legacy-tunnels.json"),
    )
    monkeypatch.setattr(palace_graph, "_get_collection", lambda *a, **kw: None)
    return tunnel_file


class TestTunnelStorage:
    def test_load_tunnels_missing_file_returns_empty_list(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        assert palace_graph._load_tunnels() == []

    def test_load_tunnels_corrupt_file_returns_empty_list(self, tmp_path, monkeypatch):
        tunnel_file = _use_tmp_tunnel_file(monkeypatch, tmp_path)
        tunnel_file.write_text("{not valid json", encoding="utf-8")
        assert palace_graph._load_tunnels() == []

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        tunnels = [
            {
                "id": "abc123",
                "source": {"wing": "wing_code", "room": "auth"},
                "target": {"wing": "wing_people", "room": "users"},
                "label": "same concept",
            }
        ]
        palace_graph._save_tunnels(tunnels)
        assert palace_graph._load_tunnels() == tunnels

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file-permission bits only apply on Unix-like systems",
    )
    def test_save_tunnels_restricts_permissions(self, tmp_path, monkeypatch):
        """Regression for #1165 — tunnels.json reveals cross-wing links and
        must not be world-readable on shared Linux/multi-user systems."""
        tunnel_file = _use_tmp_tunnel_file(monkeypatch, tmp_path)
        palace_graph._save_tunnels(
            [
                {
                    "id": "x",
                    "source": {"wing": "a", "room": "r1"},
                    "target": {"wing": "b", "room": "r2"},
                    "label": "",
                }
            ]
        )

        file_mode = stat.S_IMODE(os.stat(tunnel_file).st_mode)
        assert file_mode == 0o600, f"tunnels.json mode is {oct(file_mode)}, expected 0o600"

        parent_mode = stat.S_IMODE(os.stat(tunnel_file.parent).st_mode)
        assert parent_mode == 0o700, (
            f"tunnels.json parent dir mode is {oct(parent_mode)}, expected 0o700"
        )


class TestExplicitTunnels:
    def test_normalize_wing_uses_shared_rule_and_trims_empty(self):
        assert palace_graph._normalize_wing(" Mempalace-Public ") == "mempalace_public"
        assert palace_graph._normalize_wing("   ") is None
        assert palace_graph._normalize_wing(None) is None
        # Non-string inputs (corrupt or hand-edited tunnels.json) return None
        # instead of raising — keeps read-path filters robust to bad records.
        assert palace_graph._normalize_wing(42) is None
        assert palace_graph._normalize_wing(["x"]) is None

    def test_create_tunnel_deduplicates_reverse_order_and_updates_label(
        self, tmp_path, monkeypatch
    ):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        first = palace_graph.create_tunnel(
            "wing_code", "auth", "wing_people", "users", label="same concept"
        )
        second = palace_graph.create_tunnel(
            "wing_people", "users", "wing_code", "auth", label="updated label"
        )

        assert first["id"] == second["id"]
        assert len(palace_graph.list_tunnels()) == 1
        assert second["label"] == "updated label"
        assert second["created_at"] == first["created_at"]
        assert "updated_at" in second

    def test_create_tunnel_rejects_empty_names(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        with pytest.raises(ValueError):
            palace_graph.create_tunnel("", "auth", "wing_people", "users")

    def test_list_tunnels_filters_by_either_side(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel("wing_code", "auth", "wing_people", "users", label="A")
        palace_graph.create_tunnel("wing_ops", "deploy", "wing_people", "users", label="B")

        assert len(palace_graph.list_tunnels()) == 2
        assert len(palace_graph.list_tunnels("wing_people")) == 2
        assert len(palace_graph.list_tunnels("wing_code")) == 1

    def test_delete_tunnel_removes_saved_tunnel(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        tunnel = palace_graph.create_tunnel(
            "wing_code", "auth", "wing_people", "users", label="same concept"
        )

        assert palace_graph.delete_tunnel(tunnel["id"]) == {"deleted": tunnel["id"]}
        assert palace_graph.list_tunnels() == []

    def test_follow_tunnels_returns_direction_and_preview(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel(
            "wing_code",
            "auth",
            "wing_people",
            "users",
            label="same concept",
            target_drawer_id="drawer_users_1",
        )

        col = MagicMock()
        col.get.return_value = {
            "ids": ["drawer_users_1"],
            "documents": ["A" * 400],
            "metadatas": [{}],
        }

        outgoing = palace_graph.follow_tunnels("wing_code", "auth", col=col)
        assert len(outgoing) == 1
        assert outgoing[0]["direction"] == "outgoing"
        assert outgoing[0]["connected_wing"] == "wing_people"
        assert outgoing[0]["connected_room"] == "users"
        assert outgoing[0]["drawer_id"] == "drawer_users_1"
        assert len(outgoing[0]["drawer_preview"]) == 300

        incoming = palace_graph.follow_tunnels("wing_people", "users", col=col)
        assert len(incoming) == 1
        assert incoming[0]["direction"] == "incoming"
        assert incoming[0]["connected_wing"] == "wing_code"

    def test_follow_tunnels_returns_connections_even_if_collection_lookup_fails(
        self, tmp_path, monkeypatch
    ):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel(
            "wing_code",
            "auth",
            "wing_people",
            "users",
            label="same concept",
            target_drawer_id="drawer_users_1",
        )

        col = MagicMock()
        col.get.side_effect = RuntimeError("boom")

        connections = palace_graph.follow_tunnels("wing_code", "auth", col=col)
        assert len(connections) == 1
        assert "drawer_preview" not in connections[0]


class TestTopicTunnels:
    """Cross-wing topic tunnels (issue #1180).

    When two wings share confirmed TOPIC labels above a configurable
    threshold, a symmetric tunnel is created between them. Tunnels are
    routed through the existing ``create_tunnel`` storage so they share
    dedup and persistence with explicit tunnels.
    """

    def test_compute_topic_tunnels_creates_link_for_shared_topic(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {
            "wing_alpha": ["Angular", "OpenAPI"],
            "wing_beta": ["OpenAPI", "Kubernetes"],
        }
        created = palace_graph.compute_topic_tunnels(topics_by_wing, min_count=1)
        assert len(created) == 1
        assert created[0]["source"]["wing"] in {"wing_alpha", "wing_beta"}
        assert created[0]["target"]["wing"] in {"wing_alpha", "wing_beta"}
        # Room is namespaced with the ``topic:`` prefix so it can't collide
        # with a literal folder-derived room of the same name. Casing of the
        # topic is preserved for display.
        assert created[0]["source"]["room"] == "topic:OpenAPI"
        assert created[0]["target"]["room"] == "topic:OpenAPI"
        assert created[0]["kind"] == "topic"
        # Label carries the human-readable topic without the prefix.
        assert "OpenAPI" in created[0]["label"]
        assert "topic:OpenAPI" not in created[0]["label"]

        # Tunnel is retrievable via the standard list_tunnels API.
        listed = palace_graph.list_tunnels()
        assert len(listed) == 1
        assert listed[0]["id"] == created[0]["id"]

    def test_compute_topic_tunnels_no_link_below_threshold(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {
            "wing_alpha": ["Angular", "OpenAPI"],
            "wing_beta": ["OpenAPI", "Kubernetes"],
        }
        # min_count=2 requires two overlapping topics — only one shared.
        created = palace_graph.compute_topic_tunnels(topics_by_wing, min_count=2)
        assert created == []
        assert palace_graph.list_tunnels() == []

    def test_compute_topic_tunnels_above_threshold_creates_per_topic_links(
        self, tmp_path, monkeypatch
    ):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {
            "wing_alpha": ["Angular", "OpenAPI", "Postgres"],
            "wing_beta": ["Angular", "OpenAPI", "Redis"],
        }
        created = palace_graph.compute_topic_tunnels(topics_by_wing, min_count=2)
        # Two shared topics × one wing pair = two tunnels.
        rooms = sorted(t["source"]["room"] for t in created)
        assert rooms == ["topic:Angular", "topic:OpenAPI"]

    def test_compute_topic_tunnels_case_insensitive_overlap(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {
            "wing_alpha": ["openapi"],
            "wing_beta": ["OpenAPI"],
        }
        created = palace_graph.compute_topic_tunnels(topics_by_wing, min_count=1)
        assert len(created) == 1

    def test_compute_topic_tunnels_empty_input_is_noop(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        assert palace_graph.compute_topic_tunnels({}) == []
        assert palace_graph.compute_topic_tunnels({"wing_a": []}) == []
        assert palace_graph.list_tunnels() == []

    def test_compute_topic_tunnels_three_wings_pairwise(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {
            "wing_a": ["foo"],
            "wing_b": ["foo"],
            "wing_c": ["foo"],
        }
        created = palace_graph.compute_topic_tunnels(topics_by_wing, min_count=1)
        # 3 wings sharing the same topic → C(3,2) = 3 pairs → 3 tunnels.
        assert len(created) == 3
        endpoint_pairs = {
            tuple(sorted([t["source"]["wing"], t["target"]["wing"]])) for t in created
        }
        assert endpoint_pairs == {
            ("wing_a", "wing_b"),
            ("wing_a", "wing_c"),
            ("wing_b", "wing_c"),
        }

    def test_topic_tunnels_for_wing_only_links_that_wing(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {
            "wing_a": ["foo", "bar"],
            "wing_b": ["foo"],
            "wing_c": ["bar"],
        }
        # wing_a should link to both b (via foo) and c (via bar).
        created = palace_graph.topic_tunnels_for_wing("wing_a", topics_by_wing)
        endpoint_pairs = {
            tuple(sorted([t["source"]["wing"], t["target"]["wing"]])) for t in created
        }
        assert endpoint_pairs == {("wing_a", "wing_b"), ("wing_a", "wing_c")}
        # The b-c pair is NOT created because wing_a's incremental pass
        # only computes pairs that include wing_a.
        assert len(palace_graph.list_tunnels()) == 2

    def test_topic_tunnels_for_wing_unknown_wing_is_noop(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {"wing_a": ["foo"], "wing_b": ["foo"]}
        assert palace_graph.topic_tunnels_for_wing("wing_missing", topics_by_wing) == []
        assert palace_graph.list_tunnels() == []

    def test_topic_tunnels_for_wing_matches_across_slug_forms(self, tmp_path, monkeypatch):
        """The wing arg and ``topics_by_wing`` keys may carry different slug
        forms (hyphen vs underscore). ``topic_tunnels_for_wing`` resolves
        the lookup through ``normalize_wing_name`` so a caller passing
        ``"my-wing"`` against a registry keyed by ``"my_wing"`` still wires
        up the topic tunnels."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {"my_wing": ["Angular"], "wing_people": ["Angular"]}
        created = palace_graph.topic_tunnels_for_wing("my-wing", topics_by_wing)
        assert len(created) == 1

    def test_compute_topic_tunnels_dedupe_on_recompute(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        topics_by_wing = {
            "wing_alpha": ["OpenAPI"],
            "wing_beta": ["OpenAPI"],
        }
        first = palace_graph.compute_topic_tunnels(topics_by_wing, min_count=1)
        second = palace_graph.compute_topic_tunnels(topics_by_wing, min_count=1)
        # create_tunnel is symmetric/dedupe — repeated computation should
        # not multiply the stored tunnels.
        assert first[0]["id"] == second[0]["id"]
        assert len(palace_graph.list_tunnels()) == 1

    def test_topic_tunnel_room_does_not_collide_with_literal_room(self, tmp_path, monkeypatch):
        """Regression: a literal "Angular" folder-room and a topic tunnel
        for "Angular" must resolve to distinct endpoints so ``follow_tunnels``
        from the real room doesn't accidentally surface topic connections
        (issue raised in review of #1184)."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        # Explicit tunnel anchored at a literal "Angular" room in wing_alpha.
        palace_graph.create_tunnel(
            "wing_alpha", "Angular", "wing_gamma", "frontend", label="explicit"
        )
        # Topic tunnel between the same wings that share the "Angular" topic.
        palace_graph.compute_topic_tunnels(
            {"wing_alpha": ["Angular"], "wing_beta": ["Angular"]}, min_count=1
        )

        # follow_tunnels on the literal Angular room only sees the explicit link.
        literal = palace_graph.follow_tunnels("wing_alpha", "Angular")
        assert len(literal) == 1
        assert literal[0]["connected_wing"] == "wing_gamma"

        # The topic tunnel is stored under the namespaced room.
        topical = palace_graph.follow_tunnels("wing_alpha", "topic:Angular")
        assert len(topical) == 1
        assert topical[0]["connected_wing"] == "wing_beta"

    def test_topic_tunnels_carry_kind_field(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        palace_graph.create_tunnel("wing_a", "auth", "wing_b", "users", label="x")
        palace_graph.compute_topic_tunnels({"wing_a": ["Redis"], "wing_b": ["Redis"]}, min_count=1)

        tunnels = palace_graph.list_tunnels()
        kinds = sorted(t["kind"] for t in tunnels)
        assert kinds == ["explicit", "topic"]

    def test_compute_topic_tunnels_normalizes_wing_keys(self, tmp_path, monkeypatch):
        """Auto-generated topic tunnels canonicalize the wing slug so two
        mining runs with mixed forms (``my-wing`` then ``my_wing``) produce
        a single deduped record. Only user-issued ``create_tunnel`` calls
        preserve verbatim slugs (#1504); the topic-tunnel auto-generator
        owns its own slugs and stays canonical."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.compute_topic_tunnels(
            {"my-wing": ["Angular"], "wing_people": ["Angular"]}, min_count=1
        )
        palace_graph.compute_topic_tunnels(
            {"my_wing": ["Angular"], "wing_people": ["Angular"]}, min_count=1
        )

        tunnels = palace_graph.list_tunnels()
        assert len(tunnels) == 1
        stored_wings = {tunnels[0]["source"]["wing"], tunnels[0]["target"]["wing"]}
        assert stored_wings == {"my_wing", "wing_people"}


class TestHyphenatedWingNormalization:
    """Wing names may reach ``tunnels.json`` in either form:

    * ``mempalace mine`` without ``--wing`` derives the slug from the dir
      name through ``normalize_wing_name`` → stored as ``mempalace_public``.
    * ``mempalace mine --wing my-wing`` (or any explicit slug) is stored
      verbatim by ``create_tunnel`` (regression #1504) → ``my-wing``.

    Read-path helpers (``list_tunnels`` / ``follow_tunnels``) must accept
    queries in either form and match both storage forms — normalization
    is applied on both the stored value and the query key at comparison
    time, never at write time.
    """

    def test_list_tunnels_filters_hyphenated_wing(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel("mempalace_public", "auth", "wing_people", "users")

        assert len(palace_graph.list_tunnels("mempalace-public")) == 1
        assert len(palace_graph.list_tunnels("mempalace_public")) == 1

    def test_follow_tunnels_matches_hyphenated_wing(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel("mempalace_public", "auth", "wing_people", "users")

        by_hyphen = palace_graph.follow_tunnels("mempalace-public", "auth")
        by_under = palace_graph.follow_tunnels("mempalace_public", "auth")
        assert len(by_hyphen) == 1
        assert len(by_under) == 1
        assert by_hyphen[0]["connected_wing"] == "wing_people"

    def test_create_tunnel_preserves_hyphenated_wing_names(self, tmp_path, monkeypatch):
        """Regression for #1504: wings created via ``mempalace mine --wing my-wing``
        keep the hyphen in metadata, so ``create_tunnel`` must store the slug
        verbatim. Read-path normalization in ``list_tunnels``/``follow_tunnels``
        keeps both query forms working."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        t = palace_graph.create_tunnel("my-project", "src", "your-project", "dst", label="cross")
        assert t["source"]["wing"] == "my-project"
        assert t["target"]["wing"] == "your-project"
        assert len(palace_graph.list_tunnels("my-project")) == 1
        assert len(palace_graph.list_tunnels("my_project")) == 1

    def test_find_tunnels_warns_on_empty_result(self, tmp_path, monkeypatch, caplog):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        # No data in collection, so build_graph returns empty nodes
        with caplog.at_level("WARNING", logger="mempalace_graph"):
            result = palace_graph.find_tunnels("nonexistent-wing")
        assert result == []
        assert "No tunnels found" in caplog.text

    def test_read_path_skips_records_with_null_endpoints(self, tmp_path, monkeypatch):
        """A hand-edited ``tunnels.json`` may carry ``"source": null`` or
        ``"target": null``. The read-path filters must skip such rows
        instead of crashing the whole iteration with ``AttributeError``."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        palace_graph._save_tunnels(
            [
                {
                    "id": "broken",
                    "source": None,
                    "target": None,
                    "label": "corrupt",
                    "kind": "explicit",
                    "created_at": "2026-05-01T00:00:00+00:00",
                },
                {
                    "id": "ok",
                    "source": {"wing": "wing_a", "room": "r1"},
                    "target": {"wing": "wing_b", "room": "r2"},
                    "label": "good",
                    "kind": "explicit",
                    "created_at": "2026-05-02T00:00:00+00:00",
                },
            ]
        )

        # Both filters must skip the broken record and return the good one.
        assert {t["id"] for t in palace_graph.list_tunnels("wing_a")} == {"ok"}
        connections = palace_graph.follow_tunnels("wing_a", "r1")
        assert [c["tunnel_id"] for c in connections] == ["ok"]

    def test_pre_1504_underscore_tunnels_remain_findable(self, tmp_path, monkeypatch):
        """A ``tunnels.json`` written before #1504 stored wings in normalized
        underscore form (the write-path normalization is now gone). Read-path
        queries with either hyphen or underscore must still find those
        records after the fix."""
        tunnel_file = _use_tmp_tunnel_file(monkeypatch, tmp_path)
        palace_graph._save_tunnels(
            [
                {
                    "id": "pre_1504_record",
                    "source": {"wing": "mempalace_public", "room": "auth"},
                    "target": {"wing": "wing_people", "room": "users"},
                    "label": "pre-#1504 record",
                    "kind": "explicit",
                    "created_at": "2026-05-10T00:00:00+00:00",
                }
            ]
        )
        assert tunnel_file.exists()

        assert len(palace_graph.list_tunnels("mempalace_public")) == 1
        assert len(palace_graph.list_tunnels("mempalace-public")) == 1

        assert len(palace_graph.follow_tunnels("mempalace_public", "auth")) == 1
        assert len(palace_graph.follow_tunnels("mempalace-public", "auth")) == 1


# =============================================================================
# Regression: tunnel file follows palace_path config (#1467)
# =============================================================================
class TestTunnelFileFollowsConfig:
    """Bug A: prior to 3.3.6 the tunnel file was hardcoded at
    ``~/.mempalace/tunnels.json`` regardless of MempalaceConfig.palace_path.
    Under any profile-isolated $HOME (subagent profiles, sandboxes, multi-tenant
    hosts) tunnels would write to a different file than drawers, so
    ``create_tunnel`` would appear to succeed while the tunnel was invisible
    to every other process touching the configured palace.
    """

    def test_default_tunnel_file_unchanged(self):
        """Regression: with default config, tunnel_file resolves to
        ``~/.mempalace/tunnels.json`` so existing single-user installs are
        not silently relocated."""
        from mempalace.config import DEFAULT_PALACE_PATH, MempalaceConfig

        cfg = MempalaceConfig()
        # Default palace_path is ~/.mempalace/palace, so tunnel is sibling.
        expected = os.path.join(os.path.dirname(DEFAULT_PALACE_PATH), "tunnels.json")
        assert cfg.tunnel_file == expected
        assert palace_graph._get_tunnel_file(cfg) == expected

    def test_tunnel_file_follows_palace_path(self, tmp_path):
        """Custom palace_path → tunnel sits beside the palace, not at the
        hardcoded legacy location."""
        from mempalace.config import MempalaceConfig

        custom_dir = tmp_path / "custom-palace"
        cfg = MempalaceConfig(config_dir=tmp_path)
        cfg._file_config["palace_path"] = str(custom_dir)
        assert cfg.tunnel_file == str(tmp_path / "tunnels.json")
        assert palace_graph._get_tunnel_file(cfg) == str(tmp_path / "tunnels.json")

    def test_load_tunnels_warns_on_orphaned_legacy_file(self, tmp_path, monkeypatch, caplog):
        """When the configured tunnel file is missing but a legacy file
        exists at a different path, _load_tunnels logs a one-line warning
        naming both paths and returns []. Critically, it does NOT
        auto-migrate — silent merging risks clobbering newer data."""
        configured = tmp_path / "configured" / "tunnels.json"
        legacy = tmp_path / "legacy" / "tunnels.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            '[{"id":"orphan","source":{"wing":"a","room":"r"},'
            '"target":{"wing":"b","room":"r"},"label":""}]',
            encoding="utf-8",
        )

        monkeypatch.setattr(palace_graph, "_get_tunnel_file", lambda *a, **kw: str(configured))
        monkeypatch.setattr(palace_graph, "_legacy_tunnel_file", lambda: str(legacy))

        with caplog.at_level(logging.WARNING, logger="mempalace_graph"):
            result = palace_graph._load_tunnels()

        assert result == [], "must not auto-migrate from legacy file"
        assert str(legacy) in caplog.text
        assert str(configured) in caplog.text

    def test_no_legacy_warning_when_paths_match(self, tmp_path, monkeypatch, caplog):
        """If configured and legacy resolve to the same path (default install),
        we must not emit a misleading 'legacy file ignored' warning when the
        file simply doesn't exist yet."""
        same = tmp_path / "tunnels.json"
        monkeypatch.setattr(palace_graph, "_get_tunnel_file", lambda *a, **kw: str(same))
        monkeypatch.setattr(palace_graph, "_legacy_tunnel_file", lambda: str(same))

        with caplog.at_level(logging.WARNING, logger="mempalace_graph"):
            assert palace_graph._load_tunnels() == []

        assert "Legacy tunnels file" not in caplog.text


# =============================================================================
# Regression: create_tunnel validates explicit-tunnel endpoints (#1468)
# =============================================================================
class _StubCollection:
    """Minimal chroma-collection stub for endpoint-validation tests."""

    def __init__(self, existing_rooms):
        # existing_rooms is a set of (wing, room) tuples
        self.existing_rooms = set(existing_rooms)
        self.calls = []

    def get(self, where=None, limit=None, include=None):
        self.calls.append(where)
        # Parse the {"$and": [{"wing": W}, {"room": R}]} where clause we issue.
        wing = room = None
        for clause in (where or {}).get("$and", []):
            if "wing" in clause:
                wing = clause["wing"]
            if "room" in clause:
                room = clause["room"]
        if (wing, room) in self.existing_rooms:
            return {"ids": ["drawer-1"]}
        return {"ids": []}


class TestCreateTunnelEndpointValidation:
    """Bug B: pre-3.3.6 ``create_tunnel`` only validated wing/room names
    were non-empty strings, never that the rooms actually existed in the
    chroma index. Combined with Bug A's read-bubble, callers could
    successfully create tunnels pointing at phantom endpoints and a
    follow-up ``list_tunnels`` would self-confirm via the same isolated
    file. The fix queries chroma for at least one drawer in each endpoint
    before persisting an explicit tunnel."""

    def test_create_tunnel_rejects_nonexistent_target_room(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        col = _StubCollection({("wing_code", "auth")})  # only source exists
        monkeypatch.setattr(palace_graph, "_get_collection", lambda config=None: col)

        with pytest.raises(ValueError) as exc_info:
            palace_graph.create_tunnel("wing_code", "auth", "wing_people", "phantom")
        msg = str(exc_info.value)
        assert "phantom" in msg
        assert "wing_people" in msg
        # And nothing was persisted.
        assert palace_graph.list_tunnels() == []

    def test_create_tunnel_rejects_nonexistent_source_room(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        col = _StubCollection({("wing_people", "users")})  # only target exists
        monkeypatch.setattr(palace_graph, "_get_collection", lambda config=None: col)

        with pytest.raises(ValueError) as exc_info:
            palace_graph.create_tunnel("wing_code", "phantom", "wing_people", "users")
        msg = str(exc_info.value)
        assert "phantom" in msg
        assert "wing_code" in msg

    def test_create_tunnel_succeeds_when_both_rooms_exist(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        col = _StubCollection({("wing_code", "auth"), ("wing_people", "users")})
        monkeypatch.setattr(palace_graph, "_get_collection", lambda config=None: col)

        t = palace_graph.create_tunnel(
            "wing_code", "auth", "wing_people", "users", label="verified"
        )
        assert t["label"] == "verified"
        assert len(palace_graph.list_tunnels()) == 1

    def test_create_tunnel_skips_validation_when_collection_unreachable(
        self, tmp_path, monkeypatch
    ):
        """When chroma is unreachable (palace not yet created, transient
        failure, tests without a real backend), validation is skipped
        rather than fail-closed — matches the tolerance pattern used
        throughout palace_graph._get_collection()."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        monkeypatch.setattr(palace_graph, "_get_collection", lambda config=None: None)

        t = palace_graph.create_tunnel("wing_code", "any", "wing_people", "any", label="cold-start")
        assert t["label"] == "cold-start"

    def test_create_tunnel_tolerates_collection_query_exception(self, tmp_path, monkeypatch):
        """Permission errors / temporary chroma faults during the
        validation query must not block legitimate writes."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        class _AngryCollection:
            def get(self, **kw):
                raise RuntimeError("simulated chroma fault")

        monkeypatch.setattr(palace_graph, "_get_collection", lambda config=None: _AngryCollection())

        # Should not raise — fall back to "allow" rather than fail-closed.
        t = palace_graph.create_tunnel("wing_code", "x", "wing_people", "y", label="best-effort")
        assert t["label"] == "best-effort"

    def test_compute_topic_tunnels_skips_endpoint_validation(self, tmp_path, monkeypatch):
        """Topic tunnels use synthetic ``topic:<name>`` room identifiers
        that don't correspond to real chroma rooms. The endpoint-existence
        check must skip kind != 'explicit', otherwise auto-derived
        cross-wing graph edges would all be rejected."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        # Empty collection — would reject if validation ran.
        col = _StubCollection(set())
        monkeypatch.setattr(palace_graph, "_get_collection", lambda config=None: col)

        # Two wings share a topic — should produce a topic-tunnel even
        # though neither "topic:auth" room exists in the stub collection.
        topics_by_wing = {
            "wing_code": ["auth", "logging"],
            "wing_people": ["auth", "schema"],
        }
        palace_graph.compute_topic_tunnels(topics_by_wing, min_count=1)
        tunnels = palace_graph.list_tunnels()
        assert tunnels, "topic tunnels must persist regardless of room validation"
        assert all(t.get("kind") == "topic" for t in tunnels)
        # And the validation query was never invoked for topic tunnels.
        assert col.calls == []


class TestEntityTunnels:
    """Cross-wing entity tunnels (the Wing → Drawer-entities → Hallway →
    Tunnel sequence from the v4 architecture vision).

    When an entity has within-wing hallways in two or more wings, an entity
    tunnel bridges those wings, anchored on the entity. This is the
    architectural counterpart to ``compute_topic_tunnels`` — same storage,
    same dedup, but the substrate is hallway records (entity-grounded)
    rather than raw topic words.

    Endpoints use the synthetic room id ``entity:<name>`` so they can't
    collide with literal folder-derived rooms of the same name (mirrors
    the ``topic:<name>`` convention).

    Topic tunnels are NOT removed by this work — both systems run in
    parallel for one release cycle so existing palaces don't lose
    tunnels between mines. Deprecation is a separate follow-up PR.
    """

    def test_entity_tunnels_creates_cross_wing_tunnel_for_shared_entity(
        self, tmp_path, monkeypatch
    ):
        """Ben appears in a hallway in wing_aya AND in wing_mempalace →
        exactly one entity tunnel between those two wings, anchored on Ben."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        hallways = [
            {"wing": "wing_aya", "entity_a": "Ben", "entity_b": "Aya"},
            {"wing": "wing_mempalace", "entity_a": "Ben", "entity_b": "Igor"},
        ]
        created = palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        assert len(created) == 1
        tunnel = created[0]
        assert {tunnel["source"]["wing"], tunnel["target"]["wing"]} == {
            "wing_aya",
            "wing_mempalace",
        }
        # Endpoint is the synthetic entity room — same casing as the
        # stored entity, prefixed with ``entity:`` to prevent collision
        # with a literal folder room of the same name.
        assert tunnel["source"]["room"] == "entity:Ben"
        assert tunnel["target"]["room"] == "entity:Ben"
        assert tunnel["kind"] == "entity"
        # Label carries the entity name without the prefix.
        assert "Ben" in tunnel["label"]
        assert "entity:Ben" not in tunnel["label"]

    def test_entity_tunnels_skips_entities_in_only_one_wing(self, tmp_path, monkeypatch):
        """Aya has hallways only in wing_aya → no tunnel for her."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        hallways = [
            {"wing": "wing_aya", "entity_a": "Aya", "entity_b": "Lumi"},
            {"wing": "wing_aya", "entity_a": "Aya", "entity_b": "Ever"},
        ]
        created = palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        assert created == []

    def test_entity_tunnels_counts_entity_in_either_pair_position(self, tmp_path, monkeypatch):
        """Entity may appear as entity_a in one hallway and entity_b in
        another. Both positions count toward 'this entity is in this wing'."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        hallways = [
            # Ben is entity_b in wing_aya
            {"wing": "wing_aya", "entity_a": "Aya", "entity_b": "Ben"},
            # Ben is entity_a in wing_mempalace
            {"wing": "wing_mempalace", "entity_a": "Ben", "entity_b": "Igor"},
        ]
        created = palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        assert len(created) == 1
        assert "Ben" in created[0]["label"]

    def test_entity_tunnels_three_wings_pairwise_from_focus_wing(self, tmp_path, monkeypatch):
        """When Ben has hallways in {wing_aya, wing_mp, wing_lt} and we
        compute for wing_aya, we create wing_aya↔wing_mp AND wing_aya↔wing_lt
        only — NOT wing_mp↔wing_lt. The cross-wing tunnel between the two
        other wings is the job of whichever of those two mines next.
        """
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        hallways = [
            {"wing": "wing_aya", "entity_a": "Ben", "entity_b": "Aya"},
            {"wing": "wing_mempalace", "entity_a": "Ben", "entity_b": "Igor"},
            {"wing": "wing_lantern", "entity_a": "Ben", "entity_b": "Lumi"},
        ]
        created = palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        assert len(created) == 2
        target_wings = set()
        for t in created:
            other = (
                t["target"]["wing"] if t["source"]["wing"] == "wing_aya" else t["source"]["wing"]
            )
            target_wings.add(other)
        assert target_wings == {"wing_mempalace", "wing_lantern"}

    def test_entity_tunnels_idempotent_on_rerun(self, tmp_path, monkeypatch):
        """Re-running on the same hallway data must not duplicate tunnels.
        ``create_tunnel`` dedup-on-canonical-id is the guarantee — this test
        pins the contract."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        hallways = [
            {"wing": "wing_aya", "entity_a": "Ben", "entity_b": "Aya"},
            {"wing": "wing_mempalace", "entity_a": "Ben", "entity_b": "Igor"},
        ]
        palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        listed = palace_graph.list_tunnels()
        # Only one stored tunnel even after two passes.
        entity_tunnels = [t for t in listed if t.get("kind") == "entity"]
        assert len(entity_tunnels) == 1

    def test_entity_tunnels_retrievable_via_list_tunnels(self, tmp_path, monkeypatch):
        """Once written, entity tunnels appear in the standard
        ``list_tunnels`` query — readers don't need a separate API."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        hallways = [
            {"wing": "wing_aya", "entity_a": "Ben", "entity_b": "Aya"},
            {"wing": "wing_mempalace", "entity_a": "Ben", "entity_b": "Igor"},
        ]
        created = palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        listed = palace_graph.list_tunnels()
        assert {t["id"] for t in listed} == {t["id"] for t in created}

    def test_entity_tunnels_empty_hallways_is_noop(self, tmp_path, monkeypatch):
        """No hallways → no tunnels. No crash."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        assert palace_graph.entity_tunnels_for_wing("wing_aya", []) == []

    def test_entity_tunnels_unknown_wing_is_noop(self, tmp_path, monkeypatch):
        """Wing that doesn't appear in any hallway → no tunnels (the wing
        has no entities to bridge from)."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        hallways = [
            {"wing": "wing_other", "entity_a": "Ben", "entity_b": "Aya"},
        ]
        assert palace_graph.entity_tunnels_for_wing("wing_nonexistent", hallways) == []

    def test_entity_tunnel_room_does_not_collide_with_literal_room(self, tmp_path, monkeypatch):
        """A literal folder room ``Ben`` (created via explicit tunnel) and the
        synthetic entity room ``entity:Ben`` must produce distinct tunnels —
        not get deduped together by id collision."""
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        # Pre-create an explicit tunnel with a literal "Ben" room.
        palace_graph.create_tunnel(
            "wing_aya", "Ben", "wing_mempalace", "Ben", label="literal", kind="explicit"
        )
        hallways = [
            {"wing": "wing_aya", "entity_a": "Ben", "entity_b": "Aya"},
            {"wing": "wing_mempalace", "entity_a": "Ben", "entity_b": "Igor"},
        ]
        palace_graph.entity_tunnels_for_wing("wing_aya", hallways)
        listed = palace_graph.list_tunnels()
        # Two distinct tunnels: one literal "Ben" → "Ben", one
        # "entity:Ben" → "entity:Ben". Different canonical IDs.
        assert len(listed) == 2
        kinds = {t.get("kind") for t in listed}
        assert kinds == {"explicit", "entity"}
