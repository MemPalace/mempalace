"""Bearer-static auth: a single shared secret read from an env var."""
import os

from mempalace.auth import AuthError


class BearerStaticAuth:
    """Validate a fixed bearer token from an environment variable.

    OSS-friendly default: no external dependencies, no OIDC required.
    All clients presenting the correct token are mapped to identity 'static-client'.
    """

    def __init__(self, token_env: str = "MEMPALACE_TOKEN") -> None:
        self._token_env = token_env

    def validate(self, authorization_header: str | None) -> str:
        if not authorization_header:
            raise AuthError("missing authorization header")
        parts = authorization_header.split(" ", 1)
        if len(parts) != 2 or parts[0] != "Bearer":
            raise AuthError("unsupported authorization scheme")
        expected = os.environ.get(self._token_env)
        if not expected:
            raise AuthError("server misconfigured: token env var unset")
        if parts[1] != expected:
            raise AuthError("invalid token")
        return "static-client"
