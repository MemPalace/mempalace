"""Read a secret from the environment, or from the file ``<NAME>_FILE`` names.

Container and service managers deliver secrets as files: Compose secrets mount
at ``/run/secrets/<name>``, and systemd's ``LoadCredential=`` exposes
``$CREDENTIALS_DIRECTORY/<name>``. A secret passed that way never has to sit in
the process environment, where ``docker inspect``, ``docker compose config``
and ``/proc/<pid>/environ`` all show it.

:func:`read_secret_env` follows the convention of the official postgres, mysql
and mariadb images: ``<NAME>_FILE`` names a file holding the value of
``<NAME>``. Setting both is refused, as those images refuse it, so a value
left behind in an env file cannot quietly shadow the mounted secret.
"""

import os
from typing import Optional


class SecretEnvError(ValueError):
    """A ``<NAME>_FILE`` secret is misconfigured.

    Subclasses ``ValueError`` so startup paths that already turn a bad setting
    into a clean error (``mempalace-mcp --transport http``, the config
    fingerprint) handle it unchanged. Never swallowed into "no secret": that
    would start a server without the auth its operator configured.
    """


def secret_file_env(name: str) -> str:
    """Return the name of the variable that points at ``name``'s secret file."""
    return f"{name}_FILE"


def read_secret_env(name: str) -> Optional[str]:
    """Return the secret ``name`` from ``<name>_FILE`` or from the environment.

    - ``<name>_FILE`` set: the file's contents, trailing newlines removed.
    - Otherwise: ``os.environ.get(name)``, unchanged.

    A blank value counts as unset, so a compose file that always defines
    ``NAME: ${NAME:-}`` can still switch to ``NAME_FILE``.

    Raises :class:`SecretEnvError` when both are set, or when the file is
    unreadable, not UTF-8, or empty. Error messages name the variable and the
    path, never the file's contents, and carry no chained exception that could.
    """
    file_var = secret_file_env(name)
    path = os.environ.get(file_var, "")
    if not path.strip():
        return os.environ.get(name)
    if os.environ.get(name, "").strip():
        raise SecretEnvError(f"both {name} and {file_var} are set; set only one")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise SecretEnvError(
            f"{file_var}={path!r} could not be read: {exc.strerror or exc}"
        ) from exc
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        value = None
    if value is None:
        # Raised outside the handler: the decode error holds bytes of the
        # secret, and `from None` would still leave it on __context__.
        raise SecretEnvError(f"{file_var}={path!r} is not valid UTF-8")
    if not value.strip():
        raise SecretEnvError(f"{file_var}={path!r} is empty")
    return value
