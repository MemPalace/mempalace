"""B3 envelope primitives only: no production enrollment or content unlock.

Callers must authenticate current control state before using these primitives.
A slot authenticates bytes, not revocation, expiry, membership or API scope.
No persistence, B2 provider, operator escrow or recovery path exists here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class TokenSlotError(ValueError):
    """Malformed, mismatched or unauthenticated B3 envelope."""


_DOMAIN = b"mempalace-b3-token-slot-v2"
_RECORD_FIELDS = {"version", "key_mode", "algorithm", "workspace_id", "epoch", "slots"}
_SLOT_FIELDS = {"authority_id", "kind", "token_hash", "salt", "nonce", "ciphertext"}


def _identifier(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in value
        )
    ):
        raise TokenSlotError("Invalid B3 identifier")


def _hex(value, size):
    if (
        not isinstance(value, str)
        or len(value) != size * 2
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise TokenSlotError("Invalid B3 envelope bytes")
    return bytes.fromhex(value)


def validate_record(record):
    """Reject unknown/mixed formats and server/recovery wraps before unwrap."""
    if (
        not isinstance(record, dict)
        or set(record) != _RECORD_FIELDS
        or type(record["version"]) is not int
        or record["version"] != 2
        or record["key_mode"] != "token-v2"
        or record["algorithm"] != "HKDF-SHA256/AES-256-GCM"
        or type(record["epoch"]) is not int
        or not 1 <= record["epoch"] < 2**63
        or not isinstance(record["slots"], dict)
        or not 1 <= len(record["slots"]) <= 256
    ):
        raise TokenSlotError("Invalid B3 record")
    _identifier(record["workspace_id"])
    if not record["workspace_id"].startswith("ws_"):
        raise TokenSlotError("Invalid B3 workspace")
    for slot_id, slot in record["slots"].items():
        _identifier(slot_id)
        if slot_id in {"server", "recovery"}:
            raise TokenSlotError("Operator and recovery wraps are forbidden in B3 records")
        if not isinstance(slot, dict) or set(slot) != _SLOT_FIELDS:
            raise TokenSlotError("Invalid B3 slot")
        _identifier(slot["authority_id"])
        if slot["kind"] not in ("token", "grant"):
            raise TokenSlotError("Invalid B3 slot kind")
        for field, size in (("token_hash", 32), ("salt", 32), ("nonce", 12), ("ciphertext", 48)):
            _hex(slot[field], size)


def _token(raw_token):
    if not isinstance(raw_token, bytes) or not 32 <= len(raw_token) <= 4096:
        raise TokenSlotError("B3 requires raw random token bytes")
    return hashlib.sha256(raw_token).hexdigest()


def _kek(raw_token, salt):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_DOMAIN).derive(raw_token)


def _aad(record, slot_id, slot):
    return json.dumps(
        [
            _DOMAIN.decode("ascii"),
            record["version"],
            record["workspace_id"],
            record["epoch"],
            slot["authority_id"],
            slot["kind"],
            slot["token_hash"],
            slot_id,
        ],
        separators=(",", ":"),
    ).encode("ascii")


def wrap_dek(dek, raw_token, *, workspace_id, epoch, authority_id, kind, slot_id):
    """Wrap a caller-provided fresh workspace DEK; persist only returned bytes.

    Randomness of tokens and freshness of the workspace DEK are caller duties.
    This primitive neither enrolls a grant nor enables a workspace.
    """
    if not isinstance(dek, bytes) or len(dek) != 32:
        raise TokenSlotError("B3 requires a 256-bit workspace DEK")
    salt, nonce = os.urandom(32), os.urandom(12)
    slot = {
        "authority_id": authority_id,
        "kind": kind,
        "token_hash": _token(raw_token),
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": "00" * 48,
    }
    record = {
        "version": 2,
        "key_mode": "token-v2",
        "algorithm": "HKDF-SHA256/AES-256-GCM",
        "workspace_id": workspace_id,
        "epoch": epoch,
        "slots": {slot_id: slot},
    }
    validate_record(record)
    slot["ciphertext"] = (
        AESGCM(_kek(raw_token, salt)).encrypt(nonce, dek, _aad(record, slot_id, slot)).hex()
    )
    return record


def unwrap_dek(record, raw_token, *, workspace_id, epoch, authority_id, kind, slot_id):
    """Unwrap only after the caller has authenticated current authority.

    Expected context must come from trusted current control state, never from
    the supplied record. Disk snapshots alone cannot establish authorization.
    """
    validate_record(record)
    digest = _token(raw_token)
    slot = record["slots"].get(slot_id)
    if (
        slot is None
        or record["workspace_id"] != workspace_id
        or record["epoch"] != epoch
        or slot["authority_id"] != authority_id
        or slot["kind"] != kind
        or not hmac.compare_digest(slot["token_hash"], digest)
    ):
        raise TokenSlotError("B3 slot does not match current authority")
    try:
        return AESGCM(_kek(raw_token, _hex(slot["salt"], 32))).decrypt(
            _hex(slot["nonce"], 12),
            _hex(slot["ciphertext"], 48),
            _aad(record, slot_id, slot),
        )
    except InvalidTag:
        raise TokenSlotError("B3 slot authentication failed") from None
