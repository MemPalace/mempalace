# Social sign-in

Buttons appear after all of a provider's variables are in the gateway's operator-owned env file and the gateway restarts.
With none set, sign-in stays unchanged. Keep credentials in that file only.

Use **Commonplace** as the app name and `https://commonplace.run` as the homepage/website URL where requested. The gateway requests the scopes below.

## Google

Google Cloud Console (`console.cloud.google.com`) → select/create project → Google Auth Platform → Branding / Audience / Data Access: configure the consent screen, External audience, scopes `openid email profile`, and authorized domains `commonplace.run`, `cmmn.dev`. Clients → Create client → Web application → Authorized redirect URIs: `https://app.cmmn.dev/app/auth/google/callback`.
Client ID → `COMMONPLACE_GOOGLE_CLIENT_ID`; client secret → `COMMONPLACE_GOOGLE_CLIENT_SECRET`.
No sensitive-scope verification for these scopes; displaying the app name/logo publicly requires brand verification, including domain ownership and published homepage/privacy policy.

## GitHub

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App → Register application. Authorization callback URL: `https://app.cmmn.dev/app/auth/github/callback`; scopes `read:user user:email`.
Client ID → `COMMONPLACE_GITHUB_CLIENT_ID`; Generate a new client secret → `COMMONPLACE_GITHUB_CLIENT_SECRET`.

## Apple

Apple Developer (`developer.apple.com/account`) → Certificates, Identifiers & Profiles → Identifiers → + → App IDs: register an explicit App ID with Sign in with Apple enabled as primary. Identifiers → + → Services IDs: register, then Sign in with Apple → Configure → select that primary App ID. Domain: `app.cmmn.dev`; return URL: `https://app.cmmn.dev/app/auth/apple/callback` (POST); scopes `name email`.
Keys → + → Sign in with Apple → Configure → same primary App ID → Register → Download the `.p8` key.
Services ID → `COMMONPLACE_APPLE_CLIENT_ID`; Membership details → Team ID → `COMMONPLACE_APPLE_TEAM_ID`; key ID → `COMMONPLACE_APPLE_KEY_ID`; full `.p8` text → `COMMONPLACE_APPLE_PRIVATE_KEY` (literal `\n` is accepted).
Apple's web setup guide explicitly says domain registration needs no server file upload; `apple-developer-domain-association.txt` is not required.

## X

X Developer Console (`console.x.com`; legacy portal: `developer.x.com` → Projects & Apps) → Create App/select app → Settings → User authentication settings → Set up/Edit. Enable OAuth 2.0; choose **Web App** (confidential) and **Request email from users**; supply the published Terms of service and Privacy policy URLs. Callback/Redirect URL: `https://app.cmmn.dev/app/auth/x/callback`; scopes `tweet.read users.read users.email`.
Keys and tokens → OAuth 2.0 Client ID → `COMMONPLACE_X_CLIENT_ID`; client secret → `COMMONPLACE_X_CLIENT_SECRET` (use OAuth 2.0 credentials, not the API key/secret).
When registering, set a monthly spending cap of EUR 2 in the X developer console under billing / usage limits.
`COMMONPLACE_X_DAILY_CAP` limits X sign-ins per day (default 6; integer >= 0; 0 hides X). Empty or unset uses 6; invalid or negative values warn and use 6.
The cap resets at 00:00 UTC (01:00 CET, 02:00 CEST). When reached, the X button hides until then and returning X users are pointed to another option with the same email.

## Discord

Discord Developer Portal (`discord.com/developers/applications`) → New Application → Create → OAuth2 → Redirects → Add Redirect: `https://app.cmmn.dev/app/auth/discord/callback` → Save Changes; scopes `identify email`.
OAuth2 Client ID → `COMMONPLACE_DISCORD_CLIENT_ID`; client secret → `COMMONPLACE_DISCORD_CLIENT_SECRET`.

## Accounts and cost

After verification, a new email gets a user and personal workspace. With `COMMONPLACE_PLANS=on`, the account receives the Free plan through `commonplace/plans.py`.
New-account sign-up is closed by default. Set `COMMONPLACE_SIGNUP=open` to allow verified email and social sign-ups; unset, `closed`, or invalid values allow only existing accounts to sign in.
Known provider accounts return to the same user; a verified email matching an existing account links to it.
Unverified email never links or creates an account; an already linked provider account uses its saved identity.

Google, GitHub and Discord sign-in are free. Apple requires the paid Developer Program: US$99/year; local pricing is shown at checkout before the owner approves enrollment.
X uses prepaid API credits: one `GET /2/users/me` per sign-in is approximately US$0.01 at the published user-read rate; repeat reads of the same user within a UTC day are generally deduplicated. Confirm the rate in the console before funding.


## Rules core

Before upgrading an existing workspace, open its Rules screen and confirm the
rules are present. Startup binds the identities of its present rules and
boards stores. If a bound store later goes missing or is replaced, rules reads
return `rules_seq store_lost` and keep the previous local copy valid for recovery.
An owner can confirm intentional replacement with `POST /api/rules/store/accept`
using an empty JSON object. Review the store contents before accepting it.

Agent connections can call `rules_get format=md effective=true` for their full
authorized, approved set. The first line gives its total rule count. A complete
reply ends with `rules_seq off` when checking is disabled, or a nine-digit value
when configured for warn or enforce. `too_large`, `unavailable` and `store_lost`
markers mean the client must retain its last complete rules file.

Owners configure the workspace default or a connection override with
`PATCH /api/rules/checks`, for example `{"agent":"*","mode":"warn"}`.
Use `{"agent":"conn:builder","mode":"default"}` to remove an override.
The shared core stores these settings; connection injection, session hooks and
write enforcement are separate integrations.
