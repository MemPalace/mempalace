# Deployment notes

## Container image produced by Plan 1

- **Tag used for Plan 2 initial deploy:** `ghcr.io/gavinmcfall/mempalace@sha256:0b25f34f1bfbde78f78634e4ba49a9fc23ae4e57b8302a7b88febd59a895423b`
- **Moving tag (dev only):** `ghcr.io/gavinmcfall/mempalace:http-transport`
- **Superseded digest (earlier Task 17 build, content-equivalent):** `ghcr.io/gavinmcfall/mempalace@sha256:5826a4fcdbbe67dee0863660fab24ff316b348694eff210fbda8cab8e1772eec`

The canonical digest is the latest successful GHCR build on `feat/http-transport`. The earlier digest is preserved here for traceability; it came from the first successful Task 17 run against the same Dockerfile and source tree.

Always pin by digest in Plan 2's HelmRelease, never by moving tag.

## Plan 1 validation record

- **All tests green on branch `feat/http-transport`:** YES
- **Test count at close-out:** 715 passed, 1 deselected (benchmarks ignored)
- **Image digest at end of Plan 1:** `ghcr.io/gavinmcfall/mempalace@sha256:0b25f34f1bfbde78f78634e4ba49a9fc23ae4e57b8302a7b88febd59a895423b`
- **Date closed:** 2026-04-14
- **Suppression comments introduced:**
  - `# noqa: S310` on `urllib.request.urlopen` in `mempalace/auth/oidc_jwt.py` — intentional, documented (JWKS fetch over HTTPS with timeout).
  - `# noqa: BLE001` on the top-level `except Exception` in `mempalace/transport/stdio.py` — intentional; this is the transport main loop which must log and continue on a single malformed request rather than crash the server.
- **Commits on branch (vs upstream/develop):** 18

### Exit criteria confirmed

- [x] feat/http-transport branch pushed to gavinmcfall/mempalace
- [x] All tests pass locally and in CI
- [x] Container image pinned by sha256 digest in this file
- [x] `docker run` of pulled image responds to `/healthz` and `/mcp` correctly
- [x] Parity test passes against captured stdio reference
- [x] 100-writer stress test passes
- [x] No suppression comments introduced beyond the two documented above

Ready for Plan 2 (k8s deployment).

## Plan 1.5 hardening record

- **Date closed:** 2026-04-14
- **Ubuntu 24.04 runtime, Python 3.12**
- **Default UID:** 65534 (rootless per home-operations/containers)
- **Architectures:** linux/amd64, linux/arm64
- **readOnlyRootFilesystem verified:** YES (requires emptyDir at /tmp in k8s)
- **Palace mount path:** `/data` (PVC mount), with `MEMPALACE_PALACE_PATH=/data/palace` and `HOME=/data`
- **New image digest (canonical for Plan 2):** `ghcr.io/gavinmcfall/mempalace@sha256:53b56e8c4b54486e9bdce23a3a35606722abd0f1a01378127215eeee027c9fbd`
- **Superseded Plan 1 digest:** `ghcr.io/gavinmcfall/mempalace@sha256:0b25f34f1bfbde78f78634e4ba49a9fc23ae4e57b8302a7b88febd59a895423b`

### Adaptations applied during Plan 1.5

- Plan called for builder on `python:3.11-slim-bookworm` + runtime on Ubuntu 24.04 (Python 3.12). That cross-version setup failed at runtime with `ModuleNotFoundError: No module named 'chromadb'` because pip produced `cp311-cp311` wheels that Python 3.12 cannot import. Builder was switched to `python:3.12-slim-bookworm` to match.
- Second issue: Ubuntu's packaged `python3.12` reads from `/usr/local/lib/python3.12/dist-packages`, but `pip install --prefix=/install` writes to `site-packages`. The Dockerfile now copies `/install/bin` into `/usr/local/bin` and `/install/lib/python3.12/site-packages` into `/usr/local/lib/python3.12/dist-packages` so Ubuntu's Python resolves modules correctly.
- Volume ownership: `docker run` with a named volume created as root causes `PermissionError: [Errno 13]` when UID 65534 tries to write `/data/.mempalace`. Locally we `chown -R 65534:65534 /data` before starting the container. In k8s this is handled natively by `securityContext.fsGroup: 568`.

### K8s runtime contract (for Plan 2)

- `securityContext.runAsUser: 568` (home-ops convention) — overrides the image's 65534 default
- `securityContext.runAsGroup: 568`
- `securityContext.fsGroup: 568` — palace files will be owned by this group
- `securityContext.readOnlyRootFilesystem: true`
- `persistence.tmp`: `emptyDir` mounted at `/tmp`
- `persistence.data`: PVC mounted at `/data` (ceph-block, >=15Gi)


## Plan 2 validation record

- **Date closed:** 2026-04-14
- **Deployed image digest:** ghcr.io/gavinmcfall/mempalace@sha256:53b56e8c4b54486e9bdce23a3a35606722abd0f1a01378127215eeee027c9fbd
- **Reachable at:** https://mempalace.nerdz.cloud
- **Auth:** bearer-static (OIDC deferred to Plan 2.5)
- **Palace:** fresh empty, Ceph RBD PVC `mempalace` (15 Gi)
- **Backups:** VolSync — `mempalace` (NFS, sync window minute 25) and `mempalace-b2` (Backblaze B2, sync window minute 49); both healthy with recent successful syncs
- **MCP round-trip verified:** initialize → add_drawer → search → pod restart → search (drawer survived)
- **Bearer identity (all callers):** `static-client` — will be per-device OIDC identities in Plan 2.5

### Deviations from original plan
- Switched from OIDC-JWT to bearer-static (Plan 2.5 will revisit)
- Added ExternalSecret (onepassword-connect) pulling `mempalace` item's `MEMPALACE_TOKEN` field
- ExternalSecret shape corrected in a follow-up PR #1827 (dataFrom extract pattern, matches mem0)
- VolSync minutes: 25 / 49 (collision avoidance — see PR #1825)

### Exit criteria met
- [x] Flux reconciles cleanly
- [x] Pod 1/1 Ready
- [x] healthz OK over Cloudflare Tunnel
- [x] /mcp 401 without auth / 200 with valid token
- [x] add_drawer + search round-trip
- [x] WAL identity + schema_version
- [x] Palace persists pod restart
- [x] VolSync ReplicationSources active
- [x] /metrics Prometheus format

Ready for informal soak + Plan 2.5 (OIDC) + Plan 3 (cutover).
