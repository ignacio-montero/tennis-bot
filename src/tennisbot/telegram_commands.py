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

from .prefs import (LONDON, Prefs, PrefsError, Rule, known_centres, load_prefs,
                    parse_days, parse_time, rule_text, save_prefs, validate)

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
    "/days Tue Thu · /days any — weekdays (single-rule shorthand)\n"
    "/window 18:00-22:00 · /window 18:00- · /window any — time window\n"
    "/rules — list booking rules (priority order)\n"
    "/rule Tue Thu 18:00- · /rule Sat 10:00-15:00 · /rule Sun any — add a rule\n"
    "/rule del 2 · /rules clear — remove rule(s)\n"
    "/length 1|2 — hours per booking\n"
    "/cap 3 · /cap 0 — max PAID bookings per week (0 pauses)\n"
    "/holds 5 · /holds 0 — max concurrent UNPAID holds (0 pauses)\n"
    "/catcher on|off — arm/disarm the cancellation catcher (on asks to confirm)\n"
    "/drop on|off — arm/disarm the midnight drop (on asks to confirm)\n"
    "/live off — panic: turn BOTH off now · /live on — arm both\n"
    "/help — this list"
)


@dataclass(frozen=True)
class CommandResult:
    """What the handler decided. `prefs is None` ⇒ nothing to persist;
    `reply is None` ⇒ send nothing at all (silence for unauthorised senders)."""

    prefs: Prefs | None = None            # new config to persist, else None
    reply: str | None = None              # text to send back, else silence
    pending_confirm_until: dt.datetime | None = None   # live-handshake deadline
    # WHICH switch a pending CONFIRM will arm: "catcher" | "drop" | "both". There
    # are now two arm-able switches, so a CONFIRM must know which it applies to.
    pending_confirm_target: str | None = None
    ok: bool = True                       # False = rejected (for logging)


# Commands that manage the live-confirm handshake themselves (so the generic
# "inherit the pending state" propagation below must not clobber their result).
HANDSHAKE_CMDS = ("/live", "/catcher", "/drop")


def _strip_bot_suffix(cmd: str) -> str:
    """'/status@tennisbot' → '/status' (Telegram appends @bot in groups)."""
    return cmd.split("@", 1)[0]


def handle_message(text: str, chat_id, prefs: Prefs, *, owner_chat_id,
                   now: dt.datetime | None = None,
                   pending_confirm_until: dt.datetime | None = None,
                   pending_confirm_target: str | None = None,
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
        # Preserve the armed handshake state untouched: a stranger must neither
        # complete NOR clear it (dropping the target here would wipe the owner's
        # armed switch, and W3's fail-closed would then reject the owner's own
        # CONFIRM). The stranger is simply invisible to the state machine.
        return CommandResult(pending_confirm_until=pending_confirm_until,
                             pending_confirm_target=pending_confirm_target,
                             ok=False)

    raw = (text or "").strip()
    if not raw:
        return CommandResult(pending_confirm_until=pending_confirm_until,
                             pending_confirm_target=pending_confirm_target)

    # -- live-confirm handshake (§2.3) -------------------------------------
    # Checked BEFORE command dispatch: while a confirmation is outstanding the
    # very next message either confirms it (for the armed target) or cancels it.
    prefix = ""
    if pending_confirm_until is not None:
        if now > pending_confirm_until:
            pending_confirm_until = pending_confirm_target = None
            if raw.upper() == CONFIRM_WORD:
                return CommandResult(
                    reply="⌛ That confirmation expired — still <b>DRY-RUN</b>. "
                          "Re-send the on command if you meant it.\n"
                          + prefs.summary())
            prefix = "⌛ Live confirmation expired (still DRY-RUN).\n"
        elif raw.upper() == CONFIRM_WORD:
            if pending_confirm_target is None:
                # W3 — FAIL CLOSED: a live handshake with no target must arm
                # NOTHING (never default to "both"). Unreachable in production
                # (the session always sets until+target together), but the safe
                # default when we can't tell WHICH switch is cancel + re-prompt.
                return CommandResult(
                    reply="🚫 Couldn't tell which switch to confirm — nothing "
                          "changed. Re-send /catcher on, /drop on, or /live "
                          "on.\n" + prefs.summary(),
                    pending_confirm_until=None, pending_confirm_target=None,
                    ok=False)
            new, what = _apply_live_target(prefs, pending_confirm_target)
            return CommandResult(
                prefs=new,
                reply=f"🔴 <b>{what}</b> — real holds will be created from the "
                      f"next cycle.\n" + new.summary(),
                pending_confirm_until=None, pending_confirm_target=None)
        else:
            # Anything else cancels the arming, then is processed normally.
            pending_confirm_until = pending_confirm_target = None
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
            pending_confirm_until=pending_confirm_until,
            pending_confirm_target=pending_confirm_target, ok=False)

    if prefix and result.reply:
        result = replace(result, reply=prefix + result.reply)
    # A command that didn't set its own handshake state inherits the (possibly
    # just-cancelled) one. Handshake commands manage their own state.
    if result.pending_confirm_until is None and cmd not in HANDSHAKE_CMDS:
        result = replace(result, pending_confirm_until=pending_confirm_until,
                         pending_confirm_target=pending_confirm_target)
    return result


def _apply_live_target(prefs: Prefs, target: str) -> tuple[Prefs, str]:
    """Apply a confirmed arming to the right switch(es)."""
    if target == "catcher":
        return replace(prefs, catcher_live=True), "catcher LIVE"
    if target == "drop":
        return replace(prefs, drop_live=True), "drop LIVE"
    return replace(prefs, catcher_live=True, drop_live=True), "LIVE (both)"


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
    if cmd == "/catcher":
        return _switch("catcher", "catcher_live", args, prefs, now)
    if cmd == "/drop":
        return _switch("drop", "drop_live", args, prefs, now)

    if cmd == "/rules" and not (args and args[0].lower() == "clear"):
        return CommandResult(reply=_rules_text(prefs))   # read-only list

    # -- the config setters (§2.2) -----------------------------------------
    if cmd == "/centres":
        if not args:
            raise PrefsError("Usage: /centres paddington [westway]")
        centres = tuple(dict.fromkeys(
            a.strip().lower() for a in " ".join(args).replace(",", " ").split()))
        candidate = replace(prefs, centres=centres)
        label = "Centres: " + ", ".join(centres)

    elif cmd == "/rules":                                # only "/rules clear" here
        candidate = _with_rules(prefs, ())
        label = "Rules cleared — any day, any time."

    elif cmd == "/rule":
        candidate, label = _rule_command(args, prefs)

    elif cmd == "/days":
        if not args:
            raise PrefsError("Usage: /days Tue Thu  (or /days any)")
        candidate = _edit_sole_rule(prefs, "/days", days=parse_days(args))
        label = "Days: " + (", ".join(candidate.days) if candidate.days
                            else "any day")

    elif cmd == "/window":
        earliest, latest = _parse_window(args)
        candidate = _edit_sole_rule(prefs, "/window",
                                    earliest=earliest, latest=latest)
        label = "Window: " + candidate.window_text()

    elif cmd == "/holds":
        # `.isascii() and .isdigit()`, matching the ASCII intent (isdecimal admits
        # non-ASCII digits an IME can emit; a superscript passes isdigit but int()
        # rejects it). Consistent with /cap's guard.
        tok = args[0].lstrip("+") if args else ""
        if len(args) != 1 or not (tok.isascii() and tok.isdigit()):
            raise PrefsError("Usage: /holds 5  (whole number ≥ 0; 0 pauses "
                             "booking).")
        candidate = replace(prefs, max_holds=int(args[0]))
        label = (f"Max concurrent holds: {candidate.max_holds}"
                 + (" — booking paused" if candidate.max_holds == 0 else ""))

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


def _arm_deadline(now: dt.datetime) -> dt.datetime:
    """The confirm deadline, computed in UTC then rendered back to London.

    Adding a timedelta to a zone-aware London datetime does CIVIL arithmetic —
    on the autumn fall-back night 01:59 + 2min spans the repeated hour, giving a
    62-MINUTE window on the one guard in front of real bookings. UTC avoids it."""
    return (now.astimezone(dt.timezone.utc) + CONFIRM_TIMEOUT).astimezone(LONDON)


_ARM_MIN = int(CONFIRM_TIMEOUT.total_seconds() // 60)


def _switch(name: str, flag: str, args: list, prefs: Prefs,
            now: dt.datetime) -> CommandResult:
    """One arm-able live switch (`/catcher`, `/drop`). `off` is immediate; `on`
    arms the two-step CONFIRM handshake, tagging the pending state with `name` so
    the CONFIRM lands on the right flag."""
    arg = (args[0].lower() if args else "")
    if arg in ("off", "false", "0", "no"):
        new = replace(prefs, **{flag: False})
        return CommandResult(prefs=new,
                             reply=f"🟢 <b>{name} DRY-RUN</b> — no real {name} "
                                   f"bookings.\n" + new.summary())
    if arg in ("on", "true", "1", "yes"):
        if getattr(prefs, flag):
            return CommandResult(reply=f"{name} already <b>LIVE</b>.\n"
                                       + prefs.summary())
        return CommandResult(
            reply=f"⚠️ Enable REAL {name} bookings? Reply "
                  f"<code>{CONFIRM_WORD}</code> within {_ARM_MIN} min.",
            pending_confirm_until=_arm_deadline(now),
            pending_confirm_target=name)
    raise PrefsError(f"Usage: /{name} on  ·  /{name} off")


def _live(args: list, prefs: Prefs, now: dt.datetime) -> CommandResult:
    """`/live off` is the panic switch — turns BOTH bookers off immediately, no
    handshake (the safe direction never has a speed bump). `/live on` arms BOTH
    via the same CONFIRM handshake, tagged "both". Kept alongside the per-switch
    `/catcher`/`/drop` for the owner's muscle-memory and a one-shot arm/disarm."""
    arg = (args[0].lower() if args else "")
    if arg in ("off", "false", "0", "no"):
        new = replace(prefs, catcher_live=False, drop_live=False)
        return CommandResult(prefs=new,
                             reply="🟢 <b>DRY-RUN</b> — both bookers off.\n"
                                   + new.summary())
    if arg in ("on", "true", "1", "yes"):
        if prefs.catcher_live and prefs.drop_live:
            return CommandResult(reply="Already <b>LIVE</b> (both).\n"
                                       + prefs.summary())
        return CommandResult(
            reply=f"⚠️ Enable REAL bookings on BOTH catcher and drop? Reply "
                  f"<code>{CONFIRM_WORD}</code> within {_ARM_MIN} min.",
            pending_confirm_until=_arm_deadline(now),
            pending_confirm_target="both")
    raise PrefsError("Usage: /live on (arm both)  ·  /live off (panic: both off)")


def _with_rules(prefs: Prefs, rules) -> Prefs:
    """Set `rules` and clear the flat mirror in one step. Necessary because
    `dataclasses.replace` copies the OLD flat `days`/`earliest`/`latest` forward,
    and `Prefs.__post_init__` would then re-synthesise a rule from those stale
    fields when the new rule list is empty — so `replace(prefs, rules=())` alone
    fails to clear a window. Passing the flat fields empty lets post_init derive
    them afresh from the new rules (or leave them empty for `()`)."""
    return replace(prefs, rules=tuple(rules), days=(), earliest=None,
                   latest=None)


def _edit_sole_rule(prefs: Prefs, cmd: str, **changes) -> Prefs:
    """`/days`/`/window` shorthand: create or edit the SOLE rule. With 0 or 1
    rules this preserves today's simple single-window behaviour exactly; with ≥2
    rules it REFUSES (rather than silently destroying a multi-rule config) and
    points the owner at `/rule`/`/rules`."""
    if len(prefs.rules) >= 2:
        raise PrefsError(f"You have {len(prefs.rules)} rules, so {cmd} is "
                         f"ambiguous. Edit them with /rule / /rule del / "
                         f"/rules clear, or see /rules.")
    sole = prefs.rules[0] if prefs.rules else Rule()
    new_rule = replace(sole, **changes)
    return _with_rules(prefs, () if new_rule.is_permissive() else (new_rule,))


def _rule_command(args: list, prefs: Prefs) -> tuple[Prefs, str]:
    """`/rule <days...> <window>` (append), or `/rule del <n>` (remove)."""
    if not args:
        raise PrefsError("Usage: /rule Tue Thu 18:00-  ·  /rule Sat 10:00-15:00 "
                         "·  /rule del 2")
    if args[0].lower() == "del":
        # ASCII digits only (see /holds): isdecimal admits non-ASCII digits.
        if len(args) != 2 or not (args[1].isascii() and args[1].isdigit()):
            raise PrefsError("Usage: /rule del 2  (the number shown by /rules).")
        n = int(args[1])
        if n < 1 or n > len(prefs.rules):
            raise PrefsError(f"No rule #{n} — you have {len(prefs.rules)} "
                             f"rule(s). See /rules.")
        new_rules = prefs.rules[:n - 1] + prefs.rules[n:]
        return _with_rules(prefs, new_rules), f"Removed rule #{n}."
    # Append: the LAST token is the window, everything before it is the day list.
    *day_toks, window_tok = args
    earliest, latest = _parse_window([window_tok])
    days = parse_days(day_toks) if day_toks else ()
    rule = Rule(days=days, earliest=earliest, latest=latest)
    candidate = _with_rules(prefs, prefs.rules + (rule,))
    return candidate, f"Added rule #{len(candidate.rules)}: {rule_text(rule)}"


def _rules_text(prefs: Prefs) -> str:
    """The `/rules` read-back: the ordered rule list + a legend, or a note that
    booking is unrestricted when there are none."""
    if not prefs.rules:
        return ("No booking rules — any day, any time.\n"
                "Add one with e.g. /rule Tue Thu 18:00-\n" + prefs.summary())
    lines = ["📋 <b>Rules</b> (1 = highest priority):"]
    for i, r in enumerate(prefs.rules, 1):
        lines.append(f"{i}. {_esc(rule_text(r))}")
    lines.append(prefs.summary())
    return "\n".join(lines)


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
        (f"Bookers: catcher {'LIVE' if prefs.catcher_live else 'DRY'} · "
         f"drop {'LIVE' if prefs.drop_live else 'DRY'}"),
        f"Centres: {', '.join(prefs.centres)}",
    ]
    # 0–1 rules read as the classic Days/Window lines (backward-compatible); ≥2
    # rules render as a numbered priority list.
    if len(prefs.rules) >= 2:
        lines.append("Rules (priority order):")
        for i, r in enumerate(prefs.rules, 1):
            lines.append(f"  {i}. {_esc(rule_text(r))}")
    else:
        lines.append(f"Days: {', '.join(prefs.days) if prefs.days else 'any day'}")
        lines.append(f"Window: {prefs.window_text()}")
    lines += [
        f"Slot length: {prefs.slot_length_hours}h",
        f"Weekly cap: {cap}",
        f"Holds ceiling: {prefs.max_holds}",
        f"Next scan: {next_scan or 'unknown'}",
        f"Updated: {prefs.updated_at or 'never'} ({prefs.updated_by})",
    ]
    # W2: the midnight drop engine has no upper-time bound (only a floor), so a
    # rule's `latest` ceiling is enforced by the catcher but NOT the drop. Warn
    # only when it actually matters — the drop is armed AND some rule sets a
    # ceiling. (A proper `before_time` engine param is on the BACKLOG.)
    if prefs.drop_live and any(r.latest is not None for r in prefs.rules):
        lines.append("⚠️ Drop enforces only the window FLOOR (earliest), not the "
                     "ceiling (latest) — the catcher enforces both.")
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
        self._pending_confirm_target: str | None = None

    def handle(self, text: str, chat_id, now: dt.datetime | None = None,
               paid_this_week: int | None = None,
               next_scan: str | None = None) -> str | None:
        """Returns the reply to send, or None for silence."""
        prefs = load_prefs(self.config_dir)
        result = handle_message(
            text, chat_id, prefs,
            owner_chat_id=self.owner_chat_id, now=now,
            pending_confirm_until=self._pending_confirm_until,
            pending_confirm_target=self._pending_confirm_target,
            valid_centres=self.valid_centres,
            paid_this_week=paid_this_week, next_scan=next_scan)
        self._pending_confirm_until = result.pending_confirm_until
        self._pending_confirm_target = result.pending_confirm_target
        if result.prefs is not None:
            saved = save_prefs(result.prefs, self.config_dir,
                               updated_by="telegram")
            log.info("telegram.cmd_applied", summary=saved.summary())
        return result.reply
