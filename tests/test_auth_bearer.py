"""Tests for bearer-static auth middleware."""
import os

import pytest


@pytest.fixture(autouse=True)
def set_token(monkeypatch):
    monkeypatch.setenv("MEMPALACE_TOKEN", "s3cret")
    yield


def test_bearer_accepts_matching_token():
    from mempalace.auth.bearer_static import BearerStaticAuth

    auth = BearerStaticAuth(token_env="MEMPALACE_TOKEN")
    identity = auth.validate("Bearer s3cret")
    assert identity == "static-client"


def test_bearer_rejects_wrong_token():
    from mempalace.auth.bearer_static import BearerStaticAuth
    from mempalace.auth import AuthError

    auth = BearerStaticAuth(token_env="MEMPALACE_TOKEN")
    with pytest.raises(AuthError):
        auth.validate("Bearer wrong")


def test_bearer_rejects_missing_header():
    from mempalace.auth.bearer_static import BearerStaticAuth
    from mempalace.auth import AuthError

    auth = BearerStaticAuth(token_env="MEMPALACE_TOKEN")
    with pytest.raises(AuthError):
        auth.validate(None)


def test_bearer_rejects_non_bearer_scheme():
    from mempalace.auth.bearer_static import BearerStaticAuth
    from mempalace.auth import AuthError

    auth = BearerStaticAuth(token_env="MEMPALACE_TOKEN")
    with pytest.raises(AuthError):
        auth.validate("Basic abc123")
