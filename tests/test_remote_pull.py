"""Tests for mempalace.remote.pull — pull wing from MinIO and merge."""

import json
import os
import tempfile
import shutil
from unittest.mock import MagicMock, patch

import pytest

from mempalace.config import MempalaceConfig


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="mempalace_pull_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def minio_config(tmp_dir):
    """Config with MinIO settings."""
    cfg_dir = os.path.join(tmp_dir, "config")
    os.makedirs(cfg_dir)
    palace_path = os.path.join(tmp_dir, "palace")
    os.makedirs(palace_path)
    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump(
            {
                "palace_path": palace_path,
                "user_id": "testuser",
                "minio": {
                    "endpoint": "localhost:9000",
                    "access_key": "minioadmin",
                    "secret_key": "minioadmin",
                    "bucket": "test-bucket",
                    "secure": False,
                },
            },
            f,
        )
    return MempalaceConfig(config_dir=cfg_dir)


class TestDiscoverUsers:
    @patch("mempalace.remote.pull.get_minio_client")
    def test_lists_user_prefixes(self, mock_get_client):
        from mempalace.remote.pull import _discover_users

        mock_client = MagicMock()
        obj_a = MagicMock()
        obj_a.object_name = "proj/alice/"
        obj_b = MagicMock()
        obj_b.object_name = "proj/bob/"
        mock_client.list_objects.return_value = [obj_a, obj_b]

        users = _discover_users(mock_client, "test-bucket", "proj")
        assert users == ["alice", "bob"]


class TestPullWing:
    def test_error_when_no_minio_config(self, tmp_dir):
        cfg_dir = os.path.join(tmp_dir, "cfg_nominio")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump({}, f)
        cfg = MempalaceConfig(config_dir=cfg_dir)

        from mempalace.remote.pull import pull_wing

        result = pull_wing("proj", cfg)
        assert "error" in result

    @patch("mempalace.remote.pull.get_minio_client")
    def test_error_when_bucket_missing(self, mock_get_client, minio_config):
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = False
        mock_get_client.return_value = mock_client

        from mempalace.remote.pull import pull_wing

        result = pull_wing("proj", minio_config)
        assert "error" in result
        assert "does not exist" in result["error"]

    @patch("mempalace.remote.pull.get_minio_client")
    def test_error_when_wing_not_on_remote(self, mock_get_client, minio_config):
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True
        mock_client.list_objects.return_value = []
        mock_get_client.return_value = mock_client

        from mempalace.remote.pull import pull_wing

        result = pull_wing("nonexistent", minio_config)
        assert "error" in result
        assert "not found on remote" in result["error"]

    @patch("mempalace.remote.pull.KnowledgeGraph")
    @patch("mempalace.remote.pull.get_collection")
    @patch("mempalace.remote.pull.get_minio_client")
    def test_successful_pull_merges_drawers(
        self, mock_get_client, mock_get_col, mock_kg_cls, minio_config
    ):
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True

        obj_alice = MagicMock()
        obj_alice.object_name = "proj/alice/"
        mock_client.list_objects.return_value = [obj_alice]

        drawers_jsonl = (
            json.dumps({"id": "d1", "content": "hello world", "room": "backend", "metadata": {}})
            + "\n"
            + json.dumps(
                {"id": "d2", "content": "goodbye world", "room": "frontend", "metadata": {}}
            )
            + "\n"
        )
        kg_facts = [
            {
                "subject": "proj",
                "predicate": "uses",
                "object": "python",
                "valid_from": None,
                "valid_to": None,
            }
        ]

        def mock_get_object(bucket, path):
            resp = MagicMock()
            if path.endswith("drawers.jsonl"):
                resp.read.return_value = drawers_jsonl.encode("utf-8")
            elif path.endswith("kg_facts.json"):
                resp.read.return_value = json.dumps(kg_facts).encode("utf-8")
            elif path.endswith("manifest.json"):
                resp.read.return_value = json.dumps({"version": "1.0"}).encode("utf-8")
            return resp

        mock_client.get_object = mock_get_object
        mock_get_client.return_value = mock_client

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": []}
        mock_get_col.return_value = mock_col

        mock_kg = MagicMock()
        mock_kg.query_entity.return_value = []
        mock_kg_cls.return_value = mock_kg

        from mempalace.remote.pull import pull_wing

        result = pull_wing("proj", minio_config)

        assert result["success"] is True
        assert result["wing"] == "proj"
        assert result["sources"] == ["alice"]
        assert result["drawers_imported"] == 2
        assert result["drawers_skipped"] == 0
        assert result["kg_facts_imported"] == 1
        assert mock_col.upsert.call_count == 2

    @patch("mempalace.remote.pull.KnowledgeGraph")
    @patch("mempalace.remote.pull.get_collection")
    @patch("mempalace.remote.pull.get_minio_client")
    def test_deduplication_skips_existing(
        self, mock_get_client, mock_get_col, mock_kg_cls, minio_config
    ):
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True

        obj_alice = MagicMock()
        obj_alice.object_name = "proj/alice/"
        mock_client.list_objects.return_value = [obj_alice]

        drawers_jsonl = (
            json.dumps(
                {"id": "d1", "content": "existing content", "room": "backend", "metadata": {}}
            )
            + "\n"
        )

        def mock_get_object(bucket, path):
            resp = MagicMock()
            if path.endswith("drawers.jsonl"):
                resp.read.return_value = drawers_jsonl.encode("utf-8")
            elif path.endswith("kg_facts.json"):
                resp.read.return_value = b"[]"
            return resp

        mock_client.get_object = mock_get_object
        mock_get_client.return_value = mock_client

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": ["already_exists"]}
        mock_get_col.return_value = mock_col

        mock_kg = MagicMock()
        mock_kg_cls.return_value = mock_kg

        from mempalace.remote.pull import pull_wing

        result = pull_wing("proj", minio_config)

        assert result["success"] is True
        assert result["drawers_imported"] == 0
        assert result["drawers_skipped"] == 1
        assert mock_col.upsert.call_count == 0

    @patch("mempalace.remote.pull.KnowledgeGraph")
    @patch("mempalace.remote.pull.get_collection")
    @patch("mempalace.remote.pull.get_minio_client")
    def test_multi_user_merge(self, mock_get_client, mock_get_col, mock_kg_cls, minio_config):
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True

        obj_alice = MagicMock()
        obj_alice.object_name = "proj/alice/"
        obj_bob = MagicMock()
        obj_bob.object_name = "proj/bob/"
        mock_client.list_objects.return_value = [obj_alice, obj_bob]

        alice_drawers = (
            json.dumps({"id": "d1", "content": "alice content", "room": "r1", "metadata": {}})
            + "\n"
        )
        bob_drawers = (
            json.dumps({"id": "d2", "content": "bob content", "room": "r2", "metadata": {}}) + "\n"
        )

        def mock_get_object(bucket, path):
            resp = MagicMock()
            if "alice" in path and "drawers" in path:
                resp.read.return_value = alice_drawers.encode("utf-8")
            elif "bob" in path and "drawers" in path:
                resp.read.return_value = bob_drawers.encode("utf-8")
            elif path.endswith("kg_facts.json"):
                resp.read.return_value = b"[]"
            return resp

        mock_client.get_object = mock_get_object
        mock_get_client.return_value = mock_client

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": []}
        mock_get_col.return_value = mock_col

        mock_kg = MagicMock()
        mock_kg_cls.return_value = mock_kg

        from mempalace.remote.pull import pull_wing

        result = pull_wing("proj", minio_config)

        assert result["success"] is True
        assert result["sources"] == ["alice", "bob"]
        assert result["drawers_imported"] == 2
        assert mock_col.upsert.call_count == 2
