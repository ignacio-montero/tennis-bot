# Tennis-Bot — PRD

_What we're building and why. For the design see
[ARCHITECTURE.md](ARCHITECTURE.md); for status see
[NEXT_STEPS.md](NEXT_STEPS.md); for future ideas see [BACKLOG.md](BACKLOG.md)._

## Problem & goal

Public tennis courts in London release on a rolling 7-day window and the good
slots (evenings, weekends) sell out within minutes of the drop. Booking them
manually means remembering the drop time, being at a screen, and racing other
humans. The goal is a fully automated, hands-off bot that secures the desired
court/activity the moment it becomes available and tells me about it, so my
only job is to pay in the operator's app.

Success looks like: weeks of unattended runs where the courts/classes I want
are held for me at the drop, with a Telegram message (and screenshot) each
time, and zero silent failures.

## Target user

Just me (personal automation, own account, own bookings). Personal-scale and
polite by design: no scalping, no hoarding, no more load than a human would
generate. Shared publicly only as a portfolio piece.

## Target venues

- **Paddington Recreation Ground** and **Westway Sports Centre** — *Everyone
  Active* (Gladstone MRM/Connect backend). ✅ Implemented; any other EA centre
  is config-only.
- **Hyde Park** & **Regent's Park** — *Park Sports* (custom web platform).
  Deferred from MVP.

## MVP scope (hold-and-notify)

The bot's finish line is **securing an unpaid hold**, not paying. On Everyone
Active, selecting a slot creates an unpaid 1-hour hold visible in the user's
app; the user completes payment there. This decouples slot acquisition
(time-critical, safe to automate) from payment (no time pressure), and keeps
3DS/SCA, card storage, and PCI concerns entirely out of scope.

- [x] Book a court at a chosen centre/date/time on demand (`run-now`), dry-run
      by default, `--live` to create a real hold
- [x] Court surface preference with fallback order (e.g. Synth → Tarmac)
- [x] Optional two-consecutive-hours, same-court mode
- [x] Book activities/classes (e.g. weekly "Tennis (adv)" sessions)
- [x] Telegram notification on every outcome (success with screenshot +
      pay-in-app prompt; failures with reason) — fail safe, fail loud
- [x] Idempotency: never double-book; skip if already held/paid
- [x] Scheduled, unattended activity booking (launchd, 7 days ahead, with
      idempotent backup re-hold jobs)
- [x] Drop timing: measure the server's clock skew and spin-wait to fire at
      the release instant (`drop` command)
- [ ] Scheduled court-drop booking — built, disabled pending empirical
      confirmation of the exact drop time (see NEXT_STEPS.md)

## Explicitly out of scope

- **Payment automation / 3DS / SCA** — intentionally out of scope; a human
  pays in the app. No payment-security bypass will ever be built (see
  ARCHITECTURE.md §0 and §4.3).
- CAPTCHA-farm bypasses — if a hard challenge appears, escalate to a human via
  Telegram.
- Park Sports venues (Hyde/Regent's) — deferred, see BACKLOG.md.
- Multi-user / multi-account support.

## Success criteria

- `run-now --live` secures a real unpaid hold on demand. ✅ verified (court +
  activity; 2-hour second-hour path still needs one live confirmation)
- Scheduled activity jobs book their classes 7 days ahead unattended. ✅ live
- The drop scheduler autonomously wins a court at the 7-day drop. ⏳ pending
  drop-time confirmation
- Every run reports its outcome to Telegram — no silent failures. ✅
