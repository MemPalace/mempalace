"""Tests for OIDC JWT auth middleware (JWKS-validated)."""
import json
import time

import pytest
from authlib.jose import JsonWebKey, jwt


@pytest.fixture
def signing_key():
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    return key


@pytest.fixture
def jwks(signing_key):
    """Public JWKS dict, as served by a real OIDC provider."""
    pub = signing_key.as_dict(is_private=False)
    pub["kid"] = "test-kid"
    pub["use"] = "sig"
    pub["alg"] = "RS256"
    return {"keys": [pub]}


def _mint_token(signing_key, claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": "test-kid"}
    return jwt.encode(header, claims, signing_key).decode()


def test_oidc_accepts_valid_token(jwks, signing_key, monkeypatch):
    from mempalace.auth.oidc_jwt import OIDCJWTAuth

    auth = OIDCJWTAuth(
        issuer="https://pocket-id.test/",
        audience="mempalace-mcp",
        jwks=jwks,
    )
    token = _mint_token(signing_key, {
        "iss": "https://pocket-id.test/",
        "aud": "mempalace-mcp",
        "sub": "nerdzpc-wsl",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    })
    assert auth.validate(f"Bearer {token}") == "nerdzpc-wsl"


def test_oidc_rejects_expired_token(jwks, signing_key):
    from mempalace.auth.oidc_jwt import OIDCJWTAuth
    from mempalace.auth import AuthError

    auth = OIDCJWTAuth(
        issuer="https://pocket-id.test/",
        audience="mempalace-mcp",
        jwks=jwks,
    )
    token = _mint_token(signing_key, {
        "iss": "https://pocket-id.test/",
        "aud": "mempalace-mcp",
        "sub": "x",
        "iat": int(time.time()) - 600,
        "exp": int(time.time()) - 300,
    })
    with pytest.raises(AuthError):
        auth.validate(f"Bearer {token}")


def test_oidc_rejects_wrong_audience(jwks, signing_key):
    from mempalace.auth.oidc_jwt import OIDCJWTAuth
    from mempalace.auth import AuthError

    auth = OIDCJWTAuth(
        issuer="https://pocket-id.test/",
        audience="mempalace-mcp",
        jwks=jwks,
    )
    token = _mint_token(signing_key, {
        "iss": "https://pocket-id.test/",
        "aud": "wrong-audience",
        "sub": "x",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    })
    with pytest.raises(AuthError):
        auth.validate(f"Bearer {token}")


def test_oidc_rejects_wrong_issuer(jwks, signing_key):
    from mempalace.auth.oidc_jwt import OIDCJWTAuth
    from mempalace.auth import AuthError

    auth = OIDCJWTAuth(
        issuer="https://pocket-id.test/",
        audience="mempalace-mcp",
        jwks=jwks,
    )
    token = _mint_token(signing_key, {
        "iss": "https://attacker.test/",
        "aud": "mempalace-mcp",
        "sub": "x",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    })
    with pytest.raises(AuthError):
        auth.validate(f"Bearer {token}")
