# Recon methodology

> **Anonymised for publication.** This repository automates the author's *own*
> account on a third-party leisure-centre booking platform. The provider is not
> named here, and the concrete integration details — hostnames, endpoint paths,
> control ids, activity/site codes, and the request sequences that create a
> booking — have deliberately been removed. Publishing them would be a working
> recipe for anyone wanting to bulk-hold courts at a public amenity, which is
> not something this project wants to enable. The detailed notes are kept
> privately (`recon/FINDINGS.local.md`, gitignored).
>
> What follows is the *approach*, which is the part that's actually interesting.

## The shape of the problem

The provider runs **two independent systems** behind one brand:

1. A **modern JSON API** for auth and account data — token-based login, a
   conventional REST surface, and a single-page front end.
2. A **legacy ASP.NET WebForms booking engine**, reached by an SSO handoff from
   the account side. This is where bookings actually happen.

The second system is the hard one, and the reason the bot is built the way it is.

## Why WebForms forces browser automation

WebForms is *stateful*. Every interaction is a postback carrying a large opaque
`__VIEWSTATE` blob plus an anti-forgery token, and the control identifiers you
must target are generated per-response rather than being stable. Responses come
back as partial-update deltas, not clean documents.

That combination defeats naive HTTP replay: you cannot record one request and
fire it later, because the tokens and control ids are only valid within the
session that produced them. Replaying a captured request against a fresh session
fails.

**Decision: drive a real browser (Playwright) rather than replay raw HTTP.** The
browser maintains the session state the server expects. The cost is latency and
memory (a headless Chromium page peaks well above a plain HTTP client), which is
why the container has a much larger memory limit than the other services.

The optimisation that matters: do all the slow navigation *ahead* of the moment
that counts, so only the final confirming interaction happens at the target
instant. See `docs/ARCHITECTURE.md` §5 for the clock-skew handling that decides
when that instant actually is.

## Why the bot stops before payment

The platform supports an **unpaid hold** — the booking exists and is visible in
the provider's own app, and the user completes payment there. The bot's finish
line is that hold.

This is a deliberate scope boundary, not a limitation. Stopping before payment
removes card storage, PCI scope, and 3-D Secure / SCA from the project entirely.
The bot notifies over Telegram and the human pays in the app. See
`docs/ARCHITECTURE.md` §4.3.

Two properties of the hold were relevant to the design:

- **The hold has a generous expiry**, so a notification has ample time to be
  acted on — there is no urgency risk in the human-in-the-loop step.
- **Whether a hold is exclusive was assumed, not verified.** There was no clean
  way to test it without affecting real availability, so it is recorded as an
  accepted assumption rather than a confirmed fact.

## How the notes were produced

Browser sessions against the author's own account were captured to HAR, then
summarised by hand. Raw `.har` files and the parsed output are gitignored and
never leave the machine — they contain cookies, auth tokens, form contents, and
personal account data. See `recon/README.md` for the capture tooling and its
limits.
