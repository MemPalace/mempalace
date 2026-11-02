# Upgrade notes

New account signup now defaults to closed for both email and social sign-in.
Operators who want to accept new accounts must explicitly set
`COMMONPLACE_SIGNUP=open`. Existing users can still sign in while signup is
closed; missing or invalid settings do not enable account creation.

Email links open a confirmation page. Signing in requires an explicit button
press; opening or previewing the link does not consume it.
