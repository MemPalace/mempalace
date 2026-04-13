"""Pluggable auth middleware for the HTTP transport.

All middlewares implement the AuthMiddleware protocol and either return an identity
string (on success) or raise AuthError (on failure). The HTTP transport maps AuthError
to HTTP 401 with a structured JSON error.
"""
from typing import Protocol


class AuthError(Exception):
    """Raised when authentication fails."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AuthMiddleware(Protocol):
    """Validates an Authorization header, returns identity or raises AuthError."""

    def validate(self, authorization_header: str | None) -> str:
        ...
