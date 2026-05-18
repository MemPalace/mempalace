"""Tests for explicit tunnel helpers in mempalace.palace_graph."""

import os
import stat
import sys
from unittest.mock import MagicMock, patch

import pytest

with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    import mempalace.palace_graph as palace_graph


def _use_tmp_tunnel_file(monkeypatch, tmp_path):
    tunnel_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(palace_graph, "_TUNNEL_FILE", str(tunnel_file))
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
        assert (
            parent_mode == 0o700
        ), f"tunnels.json parent dir mode is {oct(parent_mode)}, expected 0o700"


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
