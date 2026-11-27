"""Shared helpers for the ACL tests."""

import hashlib
import json
import os

LEGACY = "legacy-admin-token"


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def write_grants(tmp_path, keys=(), mode=0o600, name="grants.json", **top):
    path = tmp_path / name
    path.write_text(json.dumps({"keys": list(keys), **top}))
    os.chmod(path, mode)
    return str(path)


def key(name, token, **kw):
    return {"name": name, "token_sha256": sha(token), **kw}
