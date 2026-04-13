# Deployment notes

## Container image produced by Plan 1

- **Tag used for Plan 2 initial deploy:** `ghcr.io/gavinmcfall/mempalace@sha256:5826a4fcdbbe67dee0863660fab24ff316b348694eff210fbda8cab8e1772eec`
- **Moving tag (dev only):** `ghcr.io/gavinmcfall/mempalace:http-transport`

Always pin by digest in Plan 2's HelmRelease, never by moving tag.
