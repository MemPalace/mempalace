## sessions-and-sign-in

- **Paths:** `commonplace/sessions.py`, `commonplace/session.py`, `commonplace/email_auth.py`, `commonplace/mailer.py`, `commonplace/capacity.py`, `commonplace/oauth/`, `commonplace/social/`
- **Owns:**

  - Authenticated consent displays the validated code destination origin and permits only that origin in form-action; login and generic pages remain self-only. IPv6 redirects fail closed because Chromium ignores IPv6 CSP host-sources.

  - Web sign-in links and cookies, startup context reads, email challenges, outgoing mail, OAuth for MCP clients and provider sign-in.

- **Promises:**

  - OAuth authentication strips loaded private/admin scope when current workspace membership is not exactly owner, independently of token scope narrowing.

  - Person private read scope requires the exact current workspace owner role. Omitted, missing and unexpected roles fail closed; members temporarily lose their own private wings too.

  - Imported-history consent: OAuth reader consent is separate and unticked; defaults and person policies exclude the reader, while refresh descendants cannot regain a dropped scope.

  - OAuth `cards:decide` consent is separate, unticked and owner-only, and requires `memory:read` plus `memory:write`; members see an owner-only notice and a forged tick grants nothing.

  - OAuth `bots:chat` and `bots:confirm` consent is separate, unticked and open to any signed-in member; `bots:confirm` is only requested together with `bots:chat`, a lone confirm tick grants nothing, and the person policy of a session never carries either scope.

  - OAuth client revoke: a workspace owner can revoke an OAuth-connected client from Settings > Agents. One transaction marks the connection, every access and refresh token and any unredeemed code revoked and audits it; later calls and refreshes are refused, and what the client wrote stays. Members, other workspaces and the client itself cannot revoke it.

  - Enabled coordinator startup context includes unsplit inbox counts, without migrating boards.

  - `session_start` takes `rules=index`: a compact list (key, section, scope, size hint, title) of the effective rules instead of full texts. The default `rules=full` is unchanged.

  - Accounts exist only after verification; sessions are cookie-bound and revocable; mail is configured only through the environment.

  - Successful email verification expires an older host-only session before setting a configured shared-domain session, so the older cookie cannot shadow the new one.

  - **Browser GitHub connection:** GitHub login reserves subject ownership, and connect-marked callbacks dispatch before login flow lookup without provisioning or replacing sessions.

  - **Account cap and waitlist:** `COMMONPLACE_MAX_ACCOUNTS` unset means unlimited and changes nothing. When set, only the creation of a new account through sign-up is gated: the count (non-disabled users) and the insert share one `BEGIN IMMEDIATE` transaction, so concurrent sign-ups cannot overshoot it, also across processes. A full server records the verified email in `signup_waitlist` and tells the person plainly (`signin=waitlist` for providers, a 200 text page for email). Existing accounts, invited members and people an instance admin admitted are never refused; admitting may exceed the cap. Admins list and admit through `GET`/`POST /api/admin/instance/waitlist` or `commonplace waitlist list|admit`.

  - **Connection lifecycle proof:** Connect-marked POST callbacks are refused before login dispatch; a normal login remains independent of an outstanding connect flow.

- **How it fails:**

  - Mail outages block new sign-ins but not existing sessions; a misconfigured provider fails closed. An invalid `COMMONPLACE_MAX_ACCOUNTS` logs one warning and leaves accounts unlimited.

- **Tests:**

  - `tests/test_commonplace_email_auth.py`, `tests/test_commonplace_oauth.py`, `tests/test_commonplace_agents_oauth_revoke.py`, `tests/test_commonplace_social.py`, `tests/test_commonplace_session.py`, `tests/test_commonplace_capacity.py`.
