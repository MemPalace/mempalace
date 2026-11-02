# Experimental workspace SQLite encryption

Encryption is opt-in and requires validation before rollout. It is not enabled
by installing this release. Existing storage stays unchanged until its owner
explicitly runs the offline migration.

The `sqlcipher` extra supplies `cryptography` and a prebuilt `sqlcipher3` driver
on Linux x86-64. Other platforms need a separately installed SQLCipher-linked
`sqlcipher3` driver. Those platforms have not been validated. A missing driver,
unsupported cipher, missing keyslot or incorrect key stops the open; there is
no plaintext fallback.

Each workspace has one random 256-bit data key, used in SQLCipher raw-key mode
for its SQLite files. Connections require 16 KB pages, HMAC-SHA256, a 64 MiB
cache and memory-only temporary storage. The compact palace, threads, boards,
rules and knowledge graph remain separate files sharing that workspace key.
This does not consolidate them into one database.

## Provisioning and migration

Provision two independent, cryptographically random 32-byte wrapping-key files
with private permissions. They are credentials, not passwords. Supply one for
the server and retain the other separately for recovery. Never put either
credential, or the key store, inside the data directory or its backup directory.

Stop the service and all workspace workers. For an existing workspace, first
complete the verified switch to compact storage. Then the ordinary workspace
owner can run:

```sh
commonplace --data DATA workspace encrypt WORKSPACE --by OWNER \
  --backup-dir NEW_BACKUP --key-store EXTERNAL_KEY_STORE \
  --server-key-file SERVER_CREDENTIAL --recovery-key-file RECOVERY_CREDENTIAL
```

The operation takes the offline lock, checks ownership, makes and verifies a
backup, exports SQLite files into a new generation, checks integrity and both
wrapped keyslots, then publishes the generation pointer. A failure before
publication leaves the original generation selected. A verified backup and the
original generation remain available for rollback. Restoring that backup into
a new data directory recovers the pre-migration state; it does not incorporate
later writes. A publication error preserves the staged files and keyslots for
manual reconciliation, including when the pointer replacement already happened.
Existing format-switch and pruning commands refuse encrypted
workspaces pending a validated recovery/retention procedure.

At runtime set `COMMONPLACE_KEY_STORE` to the external key store and
`COMMONPLACE_WRAPPING_KEY_FILE` to the server credential file. Only paths, not
key bytes, are passed to workspace workers. For recovery use the recovery
credential and set `COMMONPLACE_KEY_SLOT=recovery`. Recovery does not need the
server slot. Keyslot records use AES-256-GCM with random nonces and authenticate
the workspace id, key id, slot name and format version. No raw data key is
written into those records.

## Backups and limits

Migration records every discovered SQLite file, including unusual filenames and
nested paths, in the mandatory `encryption.database_paths` list in
`workspace.json`. The sorted paths are captured relative to the staging
generation root before in-place exports and published only after all exports
succeed. Missing or malformed inventories stop access; this experimental
format has no legacy metadata upgrade. An empty list is valid for a fresh
workspace. Entries must be unique, canonical relative POSIX paths contained in
the generation, without symlink escapes. `workspace.json` and names ending in
`-wal`, `-shm` or `-journal` are reserved and cannot be inventoried.

Inside an encrypted workspace, `.sqlite3`, `.sqlite` and `.db` are reserved
database extensions. New stores with these extensions work without updating
metadata and receive the same backup handling as migrated files. Other names
must match an inventory entry relative to the exact workspace generation.
The connection factory refuses unlisted names before loading the driver,
unwrapping a key or opening a database. Backups apply this same classification;
unexpected plaintext or corruption at a reserved or inventoried database path
fails the snapshot instead of being copied as an auxiliary file. Outside an
encrypted workspace, SQLite header detection and plaintext snapshots are
unchanged; an opaque auxiliary `.db` file can be byte-copied. Ordinary JSONL,
JSON, log and binary auxiliary files remain byte copies.

Offline backups checkpoint SQLite, acquire its writer lock, reject a remaining
WAL, and byte-copy the database. They do not use SQLite's backup API to
re-encrypt pages. Unchanged encrypted pages therefore retain their exact
ciphertext. Backup manifests verify byte counts and hashes. Encrypted files
can be verified and restored without keys; that checks their bytes, not their
decrypted integrity. SQLCipher page authentication and SQLite integrity are
checked while making the backup and again on keyed use where applicable.

Workspace settings, including this inventory, are trusted operator-controlled
metadata. The inventory is not authenticated by keyslot AAD. Malicious host
operator edits are outside this opt-in, server-decryptable model; it does not
provide operator-blind encryption.

Keyslots must be backed up separately with short retention. The application
does not yet implement that retention policy, host-key unlocking, credential
delivery, key rotation/revocation, or deletion/crypto-shredding guarantees.
Losing both slots or both wrapping credentials loses access. Old copies of
credentials and slots can still unlock old ciphertext.

This change encrypts **SQLite files only**. Revision JSONL files, entity
registries, explicit exports, operational logs, the control database and
retained pre-migration generations/backups are outside this protection. They
can contain sensitive data. It is therefore insufficient for a whole-workspace
encryption-at-rest promise. Auxiliary-file handling and the keyslot lifecycle
must be settled and validated before activation or stronger privacy claims.

SQLCipher's raw-key and export behavior follows its
[official API documentation](https://www.zetetic.net/sqlcipher/sqlcipher-api/).
