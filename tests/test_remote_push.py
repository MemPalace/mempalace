"""Tests for mempalace.remote.push — push wing to MinIO."""

import json
import os
import tempfile
import shutil
from unittest.mock import MagicMock, patch

import pytest

from mempalace.config import MempalaceConfig


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="mempalace_push_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def minio_config(tmp_dir):
    """Config with MinIO settings and user_id."""
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


class TestPushConfig:
    def test_user_id_from_config(self, minio_config):
        assert minio_config.user_id == "testuser"

    def test_user_id_from_env(self, minio_config, monkeypatch):
        monkeypatch.setenv("MEMPALACE_USER_ID", "envuser")
        assert minio_config.user_id == "envuser"

    def test_minio_endpoint(self, minio_config):
        assert minio_config.minio_endpoint == "localhost:9000"

    def test_minio_endpoint_env_override(self, minio_config, monkeypatch):
        monkeypatch.setenv("MEMPALACE_MINIO_ENDPOINT", "remote:9000")
        assert minio_config.minio_endpoint == "remote:9000"

    def test_minio_access_key(self, minio_config):
        assert minio_config.minio_access_key == "minioadmin"

    def test_minio_secret_key(self, minio_config):
        assert minio_config.minio_secret_key == "minioadmin"

    def test_minio_bucket(self, minio_config):
        assert minio_config.minio_bucket == "test-bucket"

    def test_minio_secure(self, minio_config):
        assert minio_config.minio_secure is False

    def test_minio_secure_default_true(self, tmp_dir):
        cfg_dir = os.path.join(tmp_dir, "cfg2")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump({"minio": {"endpoint": "x"}}, f)
        cfg = MempalaceConfig(config_dir=cfg_dir)
        assert cfg.minio_secure is True


class TestPushWing:
    def test_error_when_no_user_id(self, tmp_dir):
        cfg_dir = os.path.join(tmp_dir, "cfg_noid")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump({"minio": {"endpoint": "x", "access_key": "a", "secret_key": "s"}}, f)
        cfg = MempalaceConfig(config_dir=cfg_dir)

        from mempalace.remote.push import push_wing

        result = push_wing("test_wing", cfg)
        assert "error" in result
        assert "user_id" in result["error"]

    def test_error_when_no_minio_config(self, tmp_dir):
        cfg_dir = os.path.join(tmp_dir, "cfg_nominio")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump({"user_id": "bob"}, f)
        cfg = MempalaceConfig(config_dir=cfg_dir)

        from mempalace.remote.push import push_wing

        result = push_wing("test_wing", cfg)
        assert "error" in result
        assert "minio" in result["error"].lower()

    @patch("mempalace.remote.push.get_minio_client")
    @patch("mempalace.remote.push.get_collection")
    def test_error_when_wing_not_found(self, mock_get_col, mock_get_client, minio_config):
        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": []}
        mock_get_col.return_value = mock_col
        mock_get_client.return_value = MagicMock()

        from mempalace.remote.push import push_wing

        result = push_wing("nonexistent_wing", minio_config)
        assert "error" in result
        assert "not found" in result["error"]

    @patch("mempalace.remote.push.get_minio_client")
    @patch("mempalace.remote.push.get_collection")
    @patch("mempalace.remote.push._get_wing_kg_facts")
    def test_successful_push(self, mock_kg, mock_get_col, mock_get_client, minio_config):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.bucket_exists.return_value = True

        mock_col = MagicMock()
        mock_col.get.side_effect = [
            {"ids": ["drawer_1"]},
            {
                "ids": ["drawer_1", "drawer_2"],
                "documents": ["content one", "content two"],
                "metadatas": [
                    {"wing": "proj", "room": "backend", "added_by": "mcp"},
                    {"wing": "proj", "room": "frontend", "added_by": "mcp"},
                ],
            },
            {"ids": [], "documents": [], "metadatas": []},
        ]
        mock_get_col.return_value = mock_col

        mock_kg.return_value = [
            {
                "subject": "proj",
                "predicate": "uses",
                "object": "python",
                "valid_from": None,
                "valid_to": None,
            }
        ]

        from mempalace.remote.push import push_wing

        result = push_wing("proj", minio_config)

        assert result["success"] is True
        assert result["wing"] == "proj"
        assert result["user_id"] == "testuser"
        assert result["drawer_count"] == 2
        assert result["kg_fact_count"] == 1
        assert mock_client.put_object.call_count == 3
