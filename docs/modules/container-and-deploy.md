## container-and-deploy

- **Paths:** `Dockerfile`, `Dockerfile.gpu`, `docker-compose.yml`, `docker-entrypoint.sh`, `.dockerignore`, `deploy/`, `backup-hook.sh`, `nightly-backup.sh`
- **Owns:**

  - Container images, compose files, example reverse-proxy and service units, and the nightly backup wrappers.

- **Promises:**

  - Build SHA is validated package data; optional same-source RO/RW receipt binds forbid auto-creation. Gateway uses RO; only snapshot exec overrides to RW. Shared-namespace RW visibility remains explicit. Synthetic mount and package-install smoke verify the contract.

  - Images run the gateway as an unprivileged user on a mounted data directory; examples hold placeholders, never real hosts or secrets; deploy holds an active transient unit reference through fresh invocation, snapshot and backup verification, then releases it, including on trapped exits.

- **How it fails:**

  - Missing execution proof, stale snapshots, failed verification or reference creation/release refuse deployment; SIGKILL can leave a transient reference holder for operator cleanup.

  - A broken image or unit only shows at deploy time.

- **Tests:**

  - `tests/test_commonplace_deploy.py`, `tests/test_commonplace_nightly_backup.py`, `tests/test_commonplace_w5_backup.py`.
