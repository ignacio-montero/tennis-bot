"""Inbound Telegram command surface (API_SPEC §2) — pure logic, no network.

`handle_message()` is a **pure function**: (message text, sender chat id,
current prefs, handshake state) → (new prefs or None, reply or None, new
handshake state). It never touches the network and never touches the disk, so
every rule in §2 — authorisation, validation, the live-confirm handshake — is
unit-testable without a Telegram token or a filesystem.

The impure edges sit either side of it, deliberately:

- **transport** (long-poll `getUpdates`, outbound `sendMessage`) is NOT here —
  `notify/telegram.py` owns sending, and the catcher's poll loop will own
  receiving. `CommandSession.handle()` below is the seam they meet at.
- **persistence** is `prefs.save_prefs()` — called by `CommandSession`, not by
  the pure function.

Authorisation: only `TELEGRAM_CHAT_ID` is honoured; any other sender gets
`reply=None` — *silence*, not a refusal, so a stranger probing the token can't
even confirm the bot exists.
"""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import structlog

from .prefs import (LONDON, Prefs, PrefsError, known_centres, load_prefs,
                    parse_days, parse_time, save_prefs, validate)

log = structlog.get_logger()

# How long a `/live on` confirmation stays open (API_SPEC §2.3: "within 2 min").
CONFIRM_TIMEOUT = dt.timedelta(minutes=2)


def _esc(s: str) -> str:
    """Escape text before it enters an HTML-parse-mode Telegram reply.

    `notify/telegram.py` sends `parse_mode: HTML` and raise_for_status()es, so
    a single stray '<' in echoed user input makes the API return 400 and the
    reply is LOST — indistinguishable from a dead bot, in a system whose whole
    notification principle is "silence never means dead". Rejecting bad input
    and then echoing it back into markup is still injection; the fix is an
    ENCODING step at the render boundary, not more validation upstream.
    """
    return html.escape(str(s), quote=False)
CONFIRM_WORD = "CONFIRM"

HELP = (
    "🎾 <b>Tennis-Bot</b> — config commands\n"
    "/status — mode + active settings\n"
    "/centres paddington westway — where to look\n"
    "/days Tue Thu · /days any — which weekdays\n"
    "/window 18:00-22:00 · /window 18:00- · /window any — time window\n"
    "/length 1|2 — hours per booking\n"
    "/cap 3 · /cap 0 — max paid bookings per week (0 pauses booking)\n"
    "/live on — enable REAL bookings (asks you to confirm)\n"
    "/live off — back to dry-run\n"
    "/help — this list"
)


@dataclass(frozen=True)
class CommandResult:
    """What the handler decided. `prefs is None` ⇒ nothing to persist;
    `reply is None` ⇒ send nothing at all (silence for unauthorised senders)."""

    prefs: Prefs | None = None            # new config to persist, else None
    reply: str | None = None              # text to send back, else silence
    pending_confirm_until: dt.datetime | None = None   # live-handshake deadline
    ok: bool = True                       # False = rejected (for logging)


def _strip_bot_suffix(cmd: str) -> str:
    """'/status@tennisbot' → '/status' (Telegram appends @bot in groups)."""
    return cmd.split("@", 1)[0]


def handle_message(text: str, chat_id, prefs: Prefs, *, owner_chat_id,
                   now: dt.datetime | None = None,
                   pending_confirm_until: dt.datetime | None = None,
                   valid_centres: Iterable[str] | None = None,
                   paid_this_week: int | None = None,
                   next_scan: str | None = None) -> CommandResult:
    """Apply one inbound Telegram message. Pure: no I/O, no clock unless you
    omit `now`. Returns the *candidate* new prefs — the caller persists them."""
    now = now or dt.datetime.now(LONDON)
    if now.tzinfo is None:
        # A naive `now` would raise TypeError against the aware deadline below,
        # mid-handshake, and escape this function. Assume London (the system's
        # civil timezone everywhere else) rather than blowing up.
        now = now.replace(tzinfo=LONDON)

    # -- authorisation: silence, not refusal -------------------------------
    # FAIL CLOSED on a missing identity. `str(chat_id) != str(owner_chat_id)`
    # alone is a bypass: str(None) == str(None), so an absent TELEGRAM_CHAT_ID
    # (new service, new .env) combined with a sender id of None — which is what
    # the idiomatic update.get("message",{}).get("chat",{}).get("id") chain
    # yields for channel_post / edited_message / my_chat_member updates —
    # authorises a stranger against an account that creates real holds.
    # NB: strip the OWNER only (it comes from a hand-edited .env and may carry
    # stray whitespace). The sender arrives from the Telegram API as an int, so
    # stripping it too would only loosen the compare and buy nothing.
    owner = str(owner_chat_id).strip() if owner_chat_id is not None else ""
    sender = str(chat_id) if chat_id is not None else ""
    if not owner or not sender or sender != owner or owner == "None":
        log.info("telegram.cmd_ignored_foreign", chat_id=sender or "<missing>",
                 owner_configured=bool(owner) and owner != "None")
        return CommandResult(pending_confirm_until=pending_confirm_until,
                             ok=False)

    raw = (text or "").strip()
    if not raw:
        return CommandResult(pending_confirm_until=pending_confirm_until)

    # -- live-confirm handshake (§2.3) -------------------------------------
    # Checked BEFORE command dispatch: while a confirmation is outstanding the
    # very next message either confirms it or cancels it.
    prefix = ""
    if pending_confirm_until is not None:
        if now > pending_confirm_until:
            pending_confirm_until = None
            if raw.upper() == CONFIRM_WORD:
                return CommandResult(
                    reply="⌛ That confirmation expired — still <b>DRY-RUN</b>. "
                          "Send /live on again if you meant it.\n"
                          + prefs.summary())
            prefix = "⌛ Live confirmation expired (still DRY-RUN).\n"
        elif raw.upper() == CONFIRM_WORD:
            new = replace(prefs, live=True)
            return CommandResult(
                prefs=new,
                reply="🔴 <b>LIVE</b> — real holds will be created from the "
                      "next cycle.\n" + new.summary(),
                pending_confirm_until=None)
        else:
            # Anything else cancels the arming, then is processed normally.
            pending_confirm_until = None
            prefix = "🚫 Live confirmation cancelled (still DRY-RUN).\n"

    if not raw.startswith("/"):
        if raw.upper() == CONFIRM_WORD:
            return CommandResult(reply=prefix + "Nothing to confirm.",
                                 ok=False)
        return CommandResult(
            reply=prefix + "Not a command. Send /help for the list.", ok=False)

    parts = raw.split()
    cmd = _strip_bot_suffix(parts[0]).lower()
    args = parts[1:]

    try:
        result = _dispatch(cmd, args, prefs, now=now,
                           valid_centres=valid_centres,
                           paid_this_week=paid_this_week, next_scan=next_scan)
    except PrefsError as e:
        # Rejected: no new prefs are returned, so the caller never saves and
        # the previous config stays intact (PRD §7).
        log.info("telegram.cmd_rejected", cmd=cmd, error=str(e))
        return CommandResult(
            reply=prefix + f"⚠️ {_esc(str(e))}\nUnchanged: "
                           f"{_esc(prefs.summary())}",
            pending_confirm_until=pending_confirm_until, ok=False)

    if prefix and result.reply:
        result = replace(result, reply=prefix + result.reply)
    # A command that didn't set its own handshake state inherits the (possibly
    # just-cancelled) one.
    if result.pending_confirm_until is None and cmd != "/live":
        result = replace(result, pending_confirm_until=pending_confirm_until)
    return result


def _dispatch(cmd: str, args: list, prefs: Prefs, *, now: dt.datetime,
              valid_centres: Iterable[str] | None,
              paid_this_week: int | None,
              next_scan: str | None) -> CommandResult:
    if cmd == "/help" or cmd == "/start":
        return CommandResult(reply=HELP)

    if cmd == "/status":
        return CommandResult(reply=_status_text(prefs, paid_this_week,
                                                next_scan))

    if cmd == "/live":
        return _live(args, prefs, now)

    # -- the config setters (§2.2) -----------------------------------------
    if cmd == "/centres":
        if not args:
            raise PrefsError("Usage: /centres paddington [westway]")
        centres = tuple(dict.fromkeys(
            a.strip().lower() for a in " ".join(args).replace(",", " ").split()))
        candidate = replace(prefs, centres=centres)
        label = "Centres: " + ", ".join(centres)

    elif cmd == "/days":
        if not args:
            raise PrefsError("Usage: /days Tue Thu  (or /days any)")
        days = parse_days(args)
        candidate = replace(prefs, days=days)
        label = "Days: " + (", ".join(days) if days else "any day")

    elif cmd == "/window":
        earliest, latest = _parse_window(args)
        candidate = replace(prefs, earliest=earliest, latest=latest)
        label = "Window: " + candidate.window_text()

    elif cmd == "/length":
        if len(args) != 1 or args[0] not in ("1", "2"):
            raise PrefsError("Slot length must be 1 or 2 hours, e.g. /length 2.")
        candidate = replace(prefs, slot_length_hours=int(args[0]))
        label = f"Slot length: {candidate.slot_length_hours}h"

    elif cmd == "/cap":
        # `.isdecimal()`, NOT `.isdigit()`: isdigit() is True for superscripts
        # ('²', '⁵') that int() then REJECTS, so the guard and the conversion
        # disagreed and the ValueError escaped handle_message entirely. In a
        # long-poll daemon that's an unhandled exception per message.
        if len(args) != 1 or not args[0].lstrip("+").isdecimal():
            raise PrefsError("Usage: /cap 3  (whole number ≥ 0; 0 pauses "
                             "booking).")
        try:
            cap_value = int(args[0])
        except ValueError:                      # belt and braces
            raise PrefsError("Usage: /cap 3  (whole number ≥ 0).")
        candidate = replace(prefs, weekly_cap=cap_value)
        label = (f"Weekly cap: {candidate.weekly_cap}"
                 + (" — booking paused" if candidate.weekly_cap == 0 else ""))

    else:
        return CommandResult(reply=f"Unknown command {cmd}. Try /help.",
                             ok=False)

    centres_for_check = (known_centres() if valid_centres is None
                         else tuple(valid_centres))
    if cmd == "/centres" and not centres_for_check:
        # Empty means "we couldn't read targets.yaml", NOT "anything goes".
        # Skipping the membership check here let arbitrary text be persisted
        # into prefs.json, which then broke every reply that rendered it.
        raise PrefsError("Can't read the centre list right now, so I won't "
                         "change centres. Try again shortly.")
    validate(candidate, centres_for_check or None)
    # Every change replies with the new value AND the whole active config, so
    # the phone always shows the full picture (§2.2).
    return CommandResult(prefs=candidate,
                         reply=f"✅ {label}\n{candidate.summary()}")


def _live(args: list, prefs: Prefs, now: dt.datetime) -> CommandResult:
    arg = (args[0].lower() if args else "")
    if arg in ("off", "false", "0", "no"):
        new = replace(prefs, live=False)
        return CommandResult(prefs=new,
                             reply="🟢 <b>DRY-RUN</b> — no real bookings.\n"
                                   + new.summary())
    if arg in ("on", "true", "1", "yes"):
        if prefs.live:
            return CommandResult(reply="Already <b>LIVE</b>.\n"
                                       + prefs.summary())
        # Two-step: arm, don't apply. The state travels back to the caller.
        return CommandResult(
            reply=f"⚠️ Enable REAL bookings? Reply <code>{CONFIRM_WORD}</code> "
                  f"within {int(CONFIRM_TIMEOUT.total_seconds() // 60)} min.",
            # Deadline computed in UTC, not wall clock. Adding a timedelta to a
            # zone-aware London datetime does CIVIL arithmetic — on the autumn
            # fall-back night 01:59 + 2min spans the repeated hour, giving a
            # 62-MINUTE window on the one guard in front of real bookings.
            pending_confirm_until=(now.astimezone(dt.timezone.utc)
                                   + CONFIRM_TIMEOUT).astimezone(LONDON))
    raise PrefsError("Usage: /live on  ·  /live off")


def _parse_window(args: list) -> tuple:
    """'18:00-22:00' | '18:00-' | '-22:00' | '18:00 22:00' | 'any'."""
    if not args:
        raise PrefsError("Usage: /window 18:00-22:00  (or /window any)")
    spec = "".join(args).strip()
    if spec.lower() in ("any", "all", "*"):
        return None, None
    if "-" not in spec:
        raise PrefsError("Usage: /window 18:00-22:00 · /window 18:00- · "
                         "/window any")
    lo, hi = spec.split("-", 1)
    earliest = parse_time(lo, "window start") if lo.strip() else None
    latest = parse_time(hi, "window end") if hi.strip() else None
    return earliest, latest


def _status_text(prefs: Prefs, paid_this_week: int | None,
                 next_scan: str | None) -> str:
    """Read-back (§2.1) — mode FIRST, because a Telegram-set `live` flag is
    otherwise invisible persisted state (ARCHITECTURE §8.8)."""
    icon = "🔴" if prefs.live else "🟢"
    cap = f"{prefs.weekly_cap}"
    if paid_this_week is not None:
        cap = f"{paid_this_week}/{prefs.weekly_cap} paid this week"
    lines = [
        f"{icon} <b>{prefs.mode}</b>",
        f"Centres: {', '.join(prefs.centres)}",
        f"Days: {', '.join(prefs.days) if prefs.days else 'any day'}",
        f"Window: {prefs.window_text()}",
        f"Slot length: {prefs.slot_length_hours}h",
        f"Weekly cap: {cap}",
        f"Next scan: {next_scan or 'unknown'}",
        f"Updated: {prefs.updated_at or 'never'} ({prefs.updated_by})",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Stateful seam for the transport (still no network in here)
# --------------------------------------------------------------------------

class CommandSession:
    """Glue between the pure handler and the world: holds the handshake state
    between messages and persists accepted changes.

    The catcher's long-poll loop will own the actual `getUpdates` call and the
    outbound `Telegram.send` — it just feeds each message in here and sends the
    reply back if one comes out. Keeping the loop out of this class is what
    lets the whole command surface be tested with no token and no sockets.
    """

    def __init__(self, owner_chat_id, config_dir: str | Path | None = None,
                 valid_centres: Iterable[str] | None = None):
        self.owner_chat_id = str(owner_chat_id)
        self.config_dir = config_dir
        self.valid_centres = (tuple(valid_centres)
                              if valid_centres is not None else None)
        self._pending_confirm_until: dt.datetime | None = None

    def handle(self, text: str, chat_id, now: dt.datetime | None = None,
               paid_this_week: int | None = None,
               next_scan: str | None = None) -> str | None:
        """Returns the reply to send, or None for silence."""
        prefs = load_prefs(self.config_dir)
        result = handle_message(
            text, chat_id, prefs,
            owner_chat_id=self.owner_chat_id, now=now,
            pending_confirm_until=self._pending_confirm_until,
            valid_centres=self.valid_centres,
            paid_this_week=paid_this_week, next_scan=next_scan)
        self._pending_confirm_until = result.pending_confirm_until
        if result.prefs is not None:
            saved = save_prefs(result.prefs, self.config_dir,
                               updated_by="telegram")
            log.info("telegram.cmd_applied", summary=saved.summary())
        return result.reply
