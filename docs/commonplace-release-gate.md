# Fresh-user release gate

Run this journey against the candidate revision before a release, and against
develop nightly. It starts empty temporary instances on random loopback ports
and deletes their accounts, tokens and workspace files on exit. It cannot target
an existing service. No administrator token, pre-created owner, production data
or database mutation is used to set up the journey.

```sh
uv sync --extra dev
uv pip install -r scripts/requirements-release.txt
uv run --no-sync playwright install chromium
uv run --no-sync python -m scripts.commonplace_release --output /tmp/commonplace-release-report
```

Use a new output directory for every run. The default deadline is five minutes;
`--timeout` changes it. A missing browser, SDK, user route or prerequisite leaves
a failure report. Exit 0 requires every required check to pass. Exit 1 includes
both failures and blocked checks. `report.json` is machine-readable;
`report.md` is the short human report and links the screenshots. Keep artifacts
private: they show synthetic accounts and may include temporary connection UI.
No browser trace, cookie jar or bearer token is intentionally saved.
The memory worker runs unchanged; prepare its configured local embedding model
before the timed gate. A model startup/download failure does not count as a pass.

The browser uses the normal email sign-in form (`POST /app/auth/email`, HTTP 202)
and opens the captured `/app/auth/email/verify?t=…` message link (HTTP 200).
The user confirms with **Sign in** (`POST` to that URL, HTTP 303). Opening the link
alone must not create a session. It creates
a space, uses the signed-in person's connection route, connects the unmodified
Python MCP SDK, checks initialize and the prescribed startup reads, and performs
card, memory and rule operations through normal HTTP/MCP routes. It checks an
actual owner question with two options, rejects the proposing agent's attempt
to bypass that decision, and answers through the browser before completion.
The Rules page must show a summary, expand the exact full text, accept the
proposal, and supply it to a new agent session.

Two separate browser contexts/email identities prove workspace isolation, with
list, search, direct-object and MCP checks. An owner-created connection with
`read_wings: ["inbox"]` must retrieve the inbox canary and exclude another wing,
including an explicitly requested forbidden wing. Missing route support is a
blocker, never replaced by a token created inside the database.

Mail delivery is captured at the mailer boundary. A synthetic GitHub provider
tests the ordinary social start/callback path with a new identity, separately
from the required email path. This does **not** test GitHub credentials, its
remote APIs, or other real social providers. Real providers are reported as
**NOT TESTED**; add a separately approved test-account check for every configured
provider before releasing with that provider enabled. Existing offline provider
protocol tests complement this journey.

Storage and voice run on separate empty instances with smaller configured free
plan budgets: zero storage bytes and one transcription second. These change
plan configuration, never usage counters. Signup uses the normal synthetic
provider flow. Storage must refuse a real memory write with HTTP 507 and an
actionable message. Voice retains the production route/usage enforcement and
replaces only speech decoding: the first one-second PCM clip succeeds, the
second must receive a quota error with its reset time, and rejected audio must
not increase usage. Missing voice metering fails this check.

## Local release and nightly integration

The repository supplies the runnable command, not a hosted workflow. The release
runner should install the optional dependencies and browser once, check out the
candidate revision, invoke the command above in its browser test allocation,
retain the output directory, and require exit 0 before publishing. Nightly
should invoke the same command against develop with a fresh artifact directory.
Registering that command with the external local release/nightly scheduler is
still required; this change does not modify or claim to have configured it.
No GitHub Actions are added.

Fast helper verification, without starting a browser:

```sh
uv run pytest tests/test_commonplace_release_gate.py -q
```

That checks empty non-admin signup/workspace isolation, actual HTTP storage
denial, mail-link validation and failure-report behavior. It is not evidence
that the full browser/MCP release journey passed.

## Manual release checklist

Use a disposable instance and two new email addresses. Record the revision,
date, browser and any failures. Save screenshots without secrets.

- Open `/app/login` with no `t`. Sign in is visible and no session is created.
- Request an email link with the first address. Open it, check that a default
  space exists and opens as a board, then create another space.
- In Settings create a named agent. Copy its one-time token into a stock MCP
  client's HTTP configuration. Check startup rules and memory retrieval.
- Through that agent create a card, claim it, and request an owner decision with
  context and two real options. Try to finish it before the owner answers: the
  request must fail. Answer as the person, then reclaim and finish the card.
- Save a memory containing Unicode, multiple spaces and a newline. Search from
  both the agent and browser; compare the exact original text.
- Have the agent propose a shared rule with a summary. On Rules, expand its
  full text and accept it. A new agent session receives the accepted rule.
- Sign out. Confirm protected requests are anonymous. Request another email
  link and confirm the same spaces, memory and accepted rule survive login.
- In a separate private browser session sign up with the second address. It
  cannot list, search or directly open the first person's spaces, memories or
  rules. Repeat with its own MCP client.
- Restrict another agent to one wing. Confirm an allowed canary is returned and
  another wing's canary is absent from startup, search and direct requests.
- On throwaway limited-plan accounts exhaust storage and voice budgets through
  ordinary routes. Check the storage recovery and voice reset messages. Record
  any synthetic decoder or smaller plan configuration used.
- For each enabled real social provider with an authorized test account, repeat
  signup and returning login. Record unconfigured providers as NOT TESTED.
- Keep the report and screenshots. Any failed, blocked or untested required
  step holds the release until resolved; helper-test success is insufficient.
