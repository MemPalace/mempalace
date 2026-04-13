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
