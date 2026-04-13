"""OIDC JWT auth: validates bearer tokens against a JWKS endpoint.

The middleware accepts either a pre-fetched JWKS dict (for tests) or an issuer URL
from which it will fetch and cache JWKS at runtime.
"""
import time
from typing import Any
from urllib.request import urlopen

from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError

from mempalace.auth import AuthError


class OIDCJWTAuth:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks: dict[str, Any] | None = None,
        jwks_ttl_seconds: int = 3600,
    ) -> None:
        self._issuer = issuer.rstrip("/") + "/"
        self._audience = audience
        self._jwks_cache: dict[str, Any] | None = jwks
        self._jwks_ttl = jwks_ttl_seconds
        self._jwks_fetched_at = time.time() if jwks else 0.0

    def _jwks(self) -> dict[str, Any]:
        if (
            self._jwks_cache is None
            or time.time() - self._jwks_fetched_at > self._jwks_ttl
        ):
            url = self._issuer + ".well-known/jwks.json"
            with urlopen(url, timeout=5) as resp:  # noqa: S310
                import json
                self._jwks_cache = json.loads(resp.read().decode())
            self._jwks_fetched_at = time.time()
        return self._jwks_cache

    def validate(self, authorization_header: str | None) -> str:
        if not authorization_header:
            raise AuthError("missing authorization header")
        parts = authorization_header.split(" ", 1)
        if len(parts) != 2 or parts[0] != "Bearer":
            raise AuthError("unsupported authorization scheme")
        token = parts[1]
        try:
            key = JsonWebKey.import_key_set(self._jwks())
            claims = jwt.decode(
                token,
                key,
                claims_options={
                    "iss": {"essential": True, "value": self._issuer},
                    "aud": {"essential": True, "value": self._audience},
                    "exp": {"essential": True},
                },
            )
            claims.validate()
        except JoseError as e:
            raise AuthError(f"token validation failed: {e}") from e
        sub = claims.get("sub")
        if not sub:
            raise AuthError("token missing sub claim")
        return str(sub)
