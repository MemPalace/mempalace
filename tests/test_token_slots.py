"""Synthetic B3 primitive proofs; no production unlock or enrollment."""

import ast
import copy
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mempalace import token_slots as slots

TOKEN_A = b"synthetic-token-a" * 2
TOKEN_B = b"synthetic-token-b" * 2
DEK = bytes(range(32))
CONTEXT = dict(
    workspace_id="ws_synthetic", epoch=1, authority_id="pat_a", kind="token", slot_id="slot_a"
)


def envelope(token=TOKEN_A, **changes):
    return slots.wrap_dek(DEK, token, **(CONTEXT | changes))


def unlock(record, token=TOKEN_A, **changes):
    return slots.unwrap_dek(record, token, **(CONTEXT | changes))


def test_two_tokens_two_workspaces_exact_data():
    for workspace in ("ws_synthetic", "ws_other"):
        first = envelope(workspace_id=workspace)
        second = envelope(TOKEN_B, workspace_id=workspace, authority_id="pat_b", slot_id="slot_b")
        combined = copy.deepcopy(first)
        combined["slots"].update(second["slots"])
        assert unlock(combined, workspace_id=workspace) == DEK
        assert (
            unlock(
                combined, TOKEN_B, workspace_id=workspace, authority_id="pat_b", slot_id="slot_b"
            )
            == DEK
        )
        payload = b"exact words\x00\xff\xe2\x98\x83"
        cipher = AESGCM(DEK).encrypt(b"n" * 12, payload, b"synthetic")
        assert (
            AESGCM(unlock(first, workspace_id=workspace)).decrypt(b"n" * 12, cipher, b"synthetic")
            == payload
        )
        assert first != envelope(workspace_id=workspace)
        with pytest.raises(slots.TokenSlotError):
            unlock(first, TOKEN_B, workspace_id=workspace)
        with pytest.raises(slots.TokenSlotError):
            unlock(first, workspace_id="ws_wrong")


@pytest.mark.parametrize("legacy", ["server", "recovery"])
@pytest.mark.parametrize("where", ["slot", "record", "envelope"])
def test_b3_record_rejects_legacy_wraps(legacy, where):
    record = envelope()
    if where == "slot":
        record["slots"][legacy] = copy.deepcopy(record["slots"]["slot_a"])
    elif where == "record":
        record[legacy] = {"nonce": "00" * 12, "ciphertext": "00" * 48}
    else:
        record["slots"]["slot_a"][legacy] = "00" * 48
    with pytest.raises(slots.TokenSlotError):
        unlock(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", "ws_other"),
        ("epoch", 2),
        ("authority_id", "pat_other"),
        ("kind", "grant"),
        ("slot_id", "slot_other"),
    ],
)
def test_each_aad_binding(field, value):
    record = envelope()
    expected = {field: value}
    if field == "slot_id":
        record["slots"][value] = record["slots"].pop("slot_a")
    elif field in ("workspace_id", "epoch"):
        record[field] = value
    else:
        record["slots"]["slot_a"][field] = value
    # Change trusted expected context too: only ciphertext authentication can reject.
    with pytest.raises(slots.TokenSlotError):
        unlock(record, **expected)


def test_token_hash_binding_and_raw_token_kek():
    record = envelope()
    slot = record["slots"]["slot_a"]
    # Forge a valid envelope encrypted under B's raw KEK, with A's digest in AAD.
    slot["ciphertext"] = (
        AESGCM(slots._kek(TOKEN_B, bytes.fromhex(slot["salt"])))
        .encrypt(bytes.fromhex(slot["nonce"]), DEK, slots._aad(record, "slot_a", slot))
        .hex()
    )
    slot["token_hash"] = hashlib.sha256(TOKEN_B).hexdigest()
    with pytest.raises(slots.TokenSlotError):
        unlock(record, TOKEN_B)
    original = envelope()
    hashed = hashlib.sha256(TOKEN_A).digest()
    with pytest.raises(slots.TokenSlotError):
        unlock(original, hashed)
    # Even digest-derived KEK with the correct original AAD cannot decrypt.
    item = original["slots"]["slot_a"]
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        AESGCM(slots._kek(hashed, bytes.fromhex(item["salt"]))).decrypt(
            bytes.fromhex(item["nonce"]),
            bytes.fromhex(item["ciphertext"]),
            slots._aad(original, "slot_a", item),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 1),
        ("version", True),
        ("version", 3),
        ("key_mode", "operator-v1"),
        ("algorithm", "AES-256-GCM"),
        ("epoch", 0),
        ("epoch", True),
        ("workspace_id", "../ws_escape"),
        ("workspace_id", "other"),
        ("slots", {}),
        ("extra", "server"),
    ],
)
def test_strict_record(field, value):
    record = envelope()
    record[field] = value
    with pytest.raises(slots.TokenSlotError):
        slots.validate_record(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "00"),
        ("salt", "zz" * 32),
        ("ciphertext", "00" * 47),
        ("token_hash", "ff" * 32),
        ("authority_id", "../bad"),
        ("kind", "server"),
        ("extra", "escrow"),
    ],
)
def test_strict_slot(field, value):
    record = envelope()
    record["slots"]["slot_a"][field] = value
    with pytest.raises(slots.TokenSlotError):
        unlock(record)


@pytest.mark.parametrize("field", ["salt", "nonce", "ciphertext"])
def test_ciphertext_and_randomness_authenticated(field):
    record = envelope()
    item = record["slots"]["slot_a"]
    item[field] = ("00" if item[field][:2] != "00" else "01") + item[field][2:]
    with pytest.raises(slots.TokenSlotError):
        unlock(record)


def test_primitive_cannot_reach_b2_or_environment():
    tree = ast.parse(Path(slots.__file__).read_text())
    forbidden = {
        "wrap_key",
        "unwrap_key",
        "workspace_key",
        "protected_key",
        "encryption_settings",
        "connect",
        "environ",
        "getenv",
        "sqlite_crypto",
        "workspace_encryption",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(part for alias in node.names for part in alias.name.split("."))
    assert not names & forbidden


def test_independent_envelope_protocol_control():
    import json

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    record = envelope()
    item = record["slots"]["slot_a"]
    aad = json.dumps(
        [
            "mempalace-b3-token-slot-v2",
            2,
            "ws_synthetic",
            1,
            "pat_a",
            "token",
            hashlib.sha256(TOKEN_A).hexdigest(),
            "slot_a",
        ],
        separators=(",", ":"),
    ).encode("ascii")
    kek = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes.fromhex(item["salt"]),
        info=b"mempalace-b3-token-slot-v2",
    ).derive(TOKEN_A)
    assert (
        AESGCM(kek).decrypt(bytes.fromhex(item["nonce"]), bytes.fromhex(item["ciphertext"]), aad)
        == DEK
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", "ws_other"),
        ("epoch", 2),
        ("authority_id", "pat_other"),
        ("kind", "grant"),
        ("slot_id", "slot_other"),
    ],
)
def test_current_authority_mismatch(field, value):
    with pytest.raises(slots.TokenSlotError):
        unlock(envelope(), **{field: value})


def test_fresh_salt_and_nonce_and_no_secret_fields():
    import json

    first, second = envelope(), envelope()
    for field in ("salt", "nonce"):
        assert first["slots"]["slot_a"][field] != second["slots"]["slot_a"][field]
    encoded = json.dumps(first)
    assert TOKEN_A.hex() not in encoded
    assert DEK.hex() not in encoded
    assert TOKEN_A.decode() not in encoded


@pytest.mark.parametrize("token", [b"short", "x" * 32, b"x" * 4097])
def test_invalid_raw_token(token):
    with pytest.raises(slots.TokenSlotError):
        envelope(token)


@pytest.mark.parametrize("dek", [b"short", "x" * 32, b"x" * 33])
def test_invalid_dek(dek):
    with pytest.raises(slots.TokenSlotError):
        slots.wrap_dek(dek, TOKEN_A, **CONTEXT)


@pytest.mark.parametrize("value", [None, [], {}, {"version": 2}])
def test_malformed_record(value):
    with pytest.raises(slots.TokenSlotError):
        unlock(value)


def test_wrong_digest_refused_before_derive(monkeypatch):
    record = envelope()
    monkeypatch.setattr(
        slots, "_kek", lambda *args: pytest.fail("derive before token digest check")
    )
    with pytest.raises(slots.TokenSlotError):
        unlock(record, TOKEN_B)


@pytest.mark.parametrize("field,value", [("version", 2.0), ("slots", None)])
def test_strict_record_types(field, value):
    record = envelope()
    record[field] = value
    with pytest.raises(slots.TokenSlotError):
        slots.validate_record(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority_id", "../bad"),
        ("authority_id", ""),
        ("authority_id", "x" * 129),
        ("kind", "server"),
        ("nonce", "00"),
        ("nonce", "FF" * 12),
        ("salt", "zz" * 32),
        ("ciphertext", "00" * 47),
        ("extra", "escrow"),
    ],
)
def test_slot_schema_before_decrypt(field, value):
    record = envelope()
    record["slots"]["slot_a"][field] = value
    with pytest.raises(slots.TokenSlotError):
        slots.validate_record(record)
