# Security Policy

## Supported Versions

MemPalace follows semantic versioning. Security fixes land on the current major version line.

| Version            | Supported |
| ------------------ | --------- |
| 3.x (current)      | Yes       |
| 2.x and earlier    | No        |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

We take the security of MemPalace seriously. If you believe you have found a security vulnerability, please report it privately using **GitHub Private Vulnerability Reporting**:

1. Open the [Security tab](https://github.com/MemPalace/mempalace/security) of this repository.
2. Click **Advisories** → **Report a vulnerability**.
3. Fill in the form with the details below.

### What to include in your report

- A descriptive summary of the vulnerability.
- Detailed steps to reproduce the issue (including any proof-of-concept scripts or specific file paths).
- The affected version(s) and platform(s).
- The potential impact and severity.

### What to expect

- We aim to acknowledge receipt within 48 hours.
- We will triage the issue and keep you updated on progress toward a patch.
- Once the vulnerability is resolved and an update is released, we will publish a security advisory and credit you for the discovery (if you wish to be credited).

## Deployment guidance

### Do not expose a Chroma HTTP server

MemPalace's default Chroma backend uses the embedded `PersistentClient`, which
opens the palace directory in-process and listens on no network port. If you
instead run a separate `chroma run` / Chroma HTTP server (for example to share
a palace between hosts), keep it on localhost or a private network behind
authenticated access and never expose it to the public internet. Several 2026
advisories affect the Python/FastAPI Chroma server and, at the time of
writing, list no fixed release: CVE-2026-45829 (pre-authentication code
injection), CVE-2026-45833 (authenticated code injection through a collection
update), and CVE-2026-45830 and CVE-2026-45831 (tenant-authorization/RBAC
issues). They do not apply to the embedded client MemPalace uses by default.
