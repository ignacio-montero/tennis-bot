"""Orchestrates a single booking attempt (hold-and-notify).

Modes:
  court    — book a tennis court, trying enabled surfaces in preference order;
             optionally two consecutive hours on the same court.
  activity — book an Everyone Active activity (e.g. "Tennis (adv) Sun 1300").
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from playwright.sync_api import sync_playwright

from . import clock
from .config import Secrets, Target, load_targets
from .models import RunResult, Slot
from .notify.telegram import Telegram
from .providers.everyoneactive import EveryoneActiveProvider, make_context

log = structlog.get_logger()
SHOTS = Path(__file__).resolve().parents[2] / "screenshots"
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class _NullTelegram:
    """No-op notifier for dev/test runs (avoids spamming Telegram)."""
    def send(self, *a, **k): pass
    def send_photo(self, *a, **k): pass


def _weekday(date_iso: str) -> str:
    y, m, d = (int(x) for x in date_iso.split("-"))
    return _DOW[dt.date(y, m, d).weekday()]


def _next_hour(hhmm: str) -> str:
    return f"{int(hhmm[:2]) + 1:02d}:{hhmm[3:5]}"


def _next_drop(drop_local: str, tz: str, days_before: int,
               now_epoch: float | None = None,
               grace_s: float = 300.0) -> tuple[float, str]:
    """Resolve the upcoming drop instant and the date it releases.

    The drop fires at `drop_local` (HH:MM) in `tz`; the slot that opens is
    `days_before` days after the drop's OWN calendar date. This must key off the
    drop's date, not the wall-clock 'today' at launch: pre-warming at 23:56 for a
    00:00 drop is still aiming at the next day's 00:00 (and thus that day + 7).
    Returns (instant_epoch, target_date_iso).

    `grace_s` keeps a slightly-late launch (fired just after the instant) locked
    onto the drop that just happened, rather than skipping to tomorrow's.
    """
    zone = ZoneInfo(tz)
    now = now_epoch if now_epoch is not None else dt.datetime.now(zone).timestamp()
    on = dt.datetime.fromtimestamp(now, zone).date()
    instant = clock.drop_instant(drop_local, tz, on=on)
    if instant < now - grace_s:            # today's drop well past -> aim next day
        on = on + dt.timedelta(days=1)
        instant = clock.drop_instant(drop_local, tz, on=on)
    target_date = (on + dt.timedelta(days=days_before)).isoformat()
    return instant, target_date


def _append_drop_outcome(outcome: dict) -> None:
    """Append one drop outcome to $DROP_STATE_DIR/drop-outcomes.jsonl so results
    are trackable across nights (the homelab mounts a volume there). No-op when
    DROP_STATE_DIR is unset (local runs / tests)."""
    d = os.environ.get("DROP_STATE_DIR")
    if not d:
        return
    try:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        with (p / "drop-outcomes.jsonl").open("a") as f:
            f.write(json.dumps(outcome) + "\n")
    except Exception as e:                  # never let logging break a booking
        log.warn("drop.outcome_persist_failed", err=str(e))


def _candidate_times(target: Target, target_date: str,
                     want_time: str | None) -> list[str]:
    """Times to pursue, in preference order, for this date's weekday."""
    if want_time:
        return [want_time]
    wd = _weekday(target_date)
    return [p.time for p in target.want if p.day == wd]


def choose_court_slots(slots: list[Slot], target: Target,
                       want_time: str | None,
                       after_time: str | None = None) -> list[Slot]:
    """Pick the slot(s) to book from one surface's grid, honouring ranked
    preferences. Returns [] / [one] / [two] (two = consecutive, same court).
    For 2-hour mode: if the second hour isn't free on the same court, book the
    single hour (per user's choice).

    `after_time` (HH:MM) overrides ranked prefs: pursue the EARLIEST available
    slot at or after that time, any court — used by the drop dry-run rehearsal
    ('book any court after 19:00'). Times are zero-padded HH:MM so a string
    compare orders them correctly."""
    if not slots:
        return []
    avail = [s for s in slots if s.available]
    two_hours = bool(target.courts and target.courts.two_hours)
    if after_time:
        candidate_times = sorted({s.time for s in avail if s.time >= after_time})
    else:
        candidate_times = _candidate_times(target, slots[0].date, want_time)
    for t in candidate_times:
        first_choices = [s for s in avail if s.time == t]
        if not first_choices:
            continue
        if not two_hours:
            return [first_choices[0]]
        t2 = _next_hour(t)
        for s in first_choices:
            s2 = next((x for x in avail if x.time == t2 and x.court == s.court), None)
            if s2:
                return [s, s2]          # full 2-hour block, same court
        return [first_choices[0]]       # only one hour free -> take it
    return []


# ── live hold helpers ─────────────────────────────────────────────────────────
def _commit_hold(page, prov: EveryoneActiveProvider, target_date: str) -> str | None:
    """From a slot already clicked, commit the unpaid hold. Returns booking ref."""
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    return prov.confirm_hold(page)


def _hold_one(page, prov, target, surface, target_date, court, time) -> str | None:
    """Re-find a specific court+time slot on `surface` and hold it. Used for the
    second hour of a 2-hour booking (the first grid is gone after the first hold)."""
    prov.go_home(page)
    prov.search(page, site=target.site, group=target.courts.group, activity="",
                start_date=target_date, end_date=target_date)
    prov.open_timetable(page, surface.match)
    slots = prov.parse_timetable(page, target_date)
    slot = next((s for s in slots if s.available and s.court == court
                 and s.time == time), None)
    if not slot:
        raise RuntimeError(f"second-hour slot {court} {time} no longer available")
    page.click(slot.selector)
    return _commit_hold(page, prov, target_date)


# ── court mode ────────────────────────────────────────────────────────────────
def _run_court(page, ctx, prov, target, target_date, dry_run, want_time, tg,
               after_time=None):
    SHOTS.mkdir(exist_ok=True)
    notes: list[str] = []
    for surface in target.courts.ordered():
        prov.go_home(page)
        prov.search(page, site=target.site, group=target.courts.group,
                    activity="", start_date=target_date, end_date=target_date)
        try:
            prov.open_timetable(page, surface.match)
        except prov.RowFull:
            notes.append(f"{surface.label}: full"); continue
        except prov.RowMissing:
            notes.append(f"{surface.label}: not offered"); continue

        slots = prov.parse_timetable(page, target_date)
        avail_times = sorted({s.time for s in slots if s.available})
        all_times = sorted({s.time for s in slots})
        log.info("drop.grid", surface=surface.label, date=target_date,
                 n_slots=len(slots), n_avail=len(avail_times),
                 avail_times=avail_times, all_times=all_times)
        chosen = choose_court_slots(slots, target, want_time, after_time)
        if not chosen:
            notes.append(f"{surface.label}: no preferred slot "
                         f"(avail: {', '.join(avail_times) or 'none'})")
            continue

        shot = str(SHOTS / f"{target.key}-{target_date}-{surface.label}.png")
        page.screenshot(path=shot, full_page=True)
        desc = " + ".join(f"{s.time} {s.court}" for s in chosen)
        wd = _weekday(target_date)

        if dry_run:
            tg.send(f"🎾 <b>DRY-RUN</b> — {target.name} ({surface.label})\n"
                    f"✅ Would book: <b>{wd} {target_date} {desc}</b> (no hold)\n"
                    f"Other notes: {'; '.join(notes) or '—'}")
            tg.send_photo(shot, caption=f"{target.name} {surface.label} {target_date}")
            return RunResult(ok=True, dry_run=True,
                             message=f"would book {surface.label} {desc}",
                             chosen=chosen[0], screenshot_path=shot)

        # LIVE: hold the first slot, then (2h) the second.
        page.click(chosen[0].selector)
        ref1 = _commit_hold(page, prov, target_date)
        refs = [ref1]
        if len(chosen) == 2:
            try:
                refs.append(_hold_one(page, prov, target, surface, target_date,
                                      chosen[1].court, chosen[1].time))
            except Exception as e:
                log.warn("second_hour.failed", err=str(e))
                tg.send(f"⚠️ Held only the first hour ({chosen[0].time}); "
                        f"second hour failed: {e}")
        hold_shot = str(SHOTS / f"{target.key}-{target_date}-{surface.label}-HOLD.png")
        page.screenshot(path=hold_shot, full_page=True)
        tg.send(f"🎾✅ <b>HELD</b> {target.name} ({surface.label})\n"
                f"{wd} {target_date} {desc}\nRef: {', '.join(r or '?' for r in refs)}\n"
                f"💳 Open the Everyone Active app to pay (1-hour hold).")
        tg.send_photo(hold_shot, caption="Unpaid hold — pay in the app")
        return RunResult(ok=True, dry_run=False, message=f"held {surface.label} {desc}",
                         chosen=chosen[0], screenshot_path=hold_shot)

    tg.send(f"🎾 {target.name} {target_date}: nothing booked.\n"
            f"{'; '.join(notes) or 'no surfaces enabled'}")
    return RunResult(ok=True, dry_run=dry_run, message="no preferred slot; "
                     + "; ".join(notes))


# ── activity mode ─────────────────────────────────────────────────────────────
def _run_activity(page, ctx, prov, target, target_date, dry_run, activity_label, tg):
    SHOTS.mkdir(exist_ok=True)
    if not target.activities:
        return RunResult(ok=False, dry_run=dry_run,
                         message=f"{target.key} has no activities configured")
    wd = _weekday(target_date)
    items = target.activities.ordered()
    if activity_label:
        items = [i for i in items if i.label == activity_label]
    else:
        items = [i for i in items if i.day == wd]   # only activities on this weekday
    if not items:
        return RunResult(ok=False, dry_run=dry_run,
                         message=f"no enabled activity for {wd} {target_date}")

    for item in items:
        # Idempotency: skip if this session is already secured (paid or held).
        existing = prov.has_booking(page, target_date, item.match)
        if existing:
            verb = "already paid" if existing == "paid" else "already held (unpaid)"
            tg.send(f"🎾 {target.name} — {item.match} on {wd} {target_date}: "
                    f"{verb}, nothing to do.")
            return RunResult(ok=True, dry_run=dry_run,
                             message=f"{item.label}: {existing}")
        prov.go_home(page)
        prov.search(page, site=target.site, group=target.activities.group,
                    activity="", start_date=target_date, end_date=target_date)
        try:
            prov.open_timetable(page, item.match)
        except prov.RowFull:
            continue
        except prov.RowMissing:
            continue
        # Activities land on a class page (mrmClassStatus); courts on a grid.
        if "mrmClassStatus" in page.url:
            slots = prov.parse_class(page, target_date, item.time)
        else:
            slots = prov.parse_timetable(page, target_date)
        avail = [s for s in slots if s.available]
        # Prefer the slot at the activity's scheduled time; else any available.
        chosen = next((s for s in avail if s.time == item.time), None) \
            or (avail[0] if avail else None)
        shot = str(SHOTS / f"{target.key}-{target_date}-{item.label}.png")
        page.screenshot(path=shot, full_page=True)
        if not chosen:
            continue
        if dry_run:
            tg.send(f"🎾 <b>DRY-RUN</b> — {target.name} activity\n"
                    f"✅ Would book: <b>{item.match}</b> on {wd} {target_date} "
                    f"{chosen.time} (no hold)")
            tg.send_photo(shot, caption=f"{item.match} {target_date}")
            return RunResult(ok=True, dry_run=True,
                             message=f"would book activity {item.label}",
                             chosen=chosen, screenshot_path=shot)
        page.click(chosen.selector)
        ref = _commit_hold(page, prov, target_date)
        hold_shot = str(SHOTS / f"{target.key}-{target_date}-{item.label}-HOLD.png")
        page.screenshot(path=hold_shot, full_page=True)
        tg.send(f"🎾✅ <b>HELD</b> {target.name} — {item.match}\n"
                f"{wd} {target_date} {chosen.time}\nRef: {ref or '?'}\n"
                f"💳 Open the Everyone Active app to pay (1-hour hold).")
        tg.send_photo(hold_shot, caption="Unpaid hold — pay in the app")
        return RunResult(ok=True, dry_run=False, message=f"held activity {item.label}",
                         chosen=chosen, screenshot_path=hold_shot)

    tg.send(f"🎾 {target.name} {target_date}: no activity slot available.")
    return RunResult(ok=True, dry_run=dry_run, message="no activity slot")


# ── timed court-drop entrypoint ────────────────────────────────────────────────
def run_drop(target_key: str = "paddington", dry_run: bool = True,
             headless: bool = True, want_time: str | None = None,
             time_override: str | None = None, notify: bool = True,
             epsilon: float = 0.15, retry_window_s: float = 90.0,
             retry_gap_s: float = 1.0,
             after_time: str | None = None) -> RunResult:
    """Pre-warm a session, spin-wait to the server-clock drop instant, then
    'camp' the booking: retry rapidly through overload / not-yet-dropped until it
    books or `retry_window_s` elapses. Targets the date the drop releases
    (drop date + days_before — see _next_drop for the midnight-rollover reason).

    `after_time` (HH:MM) books the earliest available court at/after that time,
    any court — used by the dry-run rehearsal ('book any court after 19:00').
    """
    import time as _time
    secrets = Secrets.from_env()
    target = load_targets()[target_key]
    tg = (Telegram(secrets.telegram_bot_token, secrets.telegram_chat_id)
          if notify else _NullTelegram())
    drop_local = time_override or target.drop.local_time
    instant, target_date = _next_drop(drop_local, target.drop.timezone,
                                      target.drop.days_before)

    with sync_playwright() as p:
        browser, ctx = make_context(p, headless=headless)
        page = ctx.new_page()
        prov = EveryoneActiveProvider(secrets, target)
        try:
            prov.start_session(ctx, page)      # pre-warm: authenticate early
            prov.enter_connect(page, ctx)
            skew = clock.server_skew()
            log.info("drop.armed", date=target_date, drop_local=drop_local,
                     skew=round(skew, 3), after_time=after_time)
            tg.send(f"⏳ Armed: {target.name} court drop {drop_local} "
                    f"(for {target_date}). Server skew {skew:+.2f}s.")
            clock.wait_until(instant, skew=skew, epsilon=epsilon)

            # Camp the drop: retry through overload/errors and "not dropped yet"
            # until we secure a slot or the window closes. Telegram is suppressed
            # during attempts (via a quiet notifier); we notify once at the end.
            quiet = _NullTelegram()
            deadline = _time.time() + retry_window_s
            attempts = 0
            result = None
            while _time.time() < deadline:
                attempts += 1
                try:
                    result = _run_court(page, ctx, prov, target, target_date,
                                        dry_run, want_time, quiet, after_time)
                except Exception as e:                       # overload / timeout
                    log.warn("drop.attempt_failed", n=attempts, err=str(e)[:120])
                    _time.sleep(retry_gap_s)
                    continue
                if result.chosen is not None:                # secured / would-book
                    log.info("drop.secured", attempts=attempts)
                    break
                _time.sleep(retry_gap_s)                      # not up yet — retry

            def _record(secured, chosen_slot, msg):
                """One structured summary line + a persisted outcome, so a
                triggered run's result is visible in logs AND trackable across
                nights (drop-outcomes.jsonl). `secured` = did we book / would-book
                a slot? (Distinct from the CLI's exit-ok = 'ran without error'.)"""
                outcome = {
                    "ts": dt.datetime.now(ZoneInfo(target.drop.timezone)).isoformat(),
                    "target": target.key, "target_date": target_date,
                    "drop_local": drop_local, "after_time": after_time,
                    "skew": round(skew, 3), "attempts": attempts,
                    "dry_run": dry_run, "secured": secured,
                    "chosen": (f"{chosen_slot.time} {chosen_slot.court}"
                               if chosen_slot else None),
                    "message": msg,
                }
                log.info("drop.result", **outcome)
                _append_drop_outcome(outcome)

            if result is not None and result.chosen is not None:
                s = result.chosen
                _record(True, s,
                        f"{'would book' if dry_run else 'held'} {s.time} {s.court}")
                if dry_run:
                    tg.send(f"🎾 <b>DRY-RUN drop</b> — {target.name}: would book "
                            f"{s.time} {s.court} on {target_date} "
                            f"(after {attempts} attempt(s)).")
                else:
                    tg.send(f"🎾✅ <b>HELD (drop)</b> — {target.name}: {s.time} "
                            f"{s.court} on {target_date} (after {attempts} "
                            f"attempt(s)).\n💳 Pay in the Everyone Active app.")
                    if result.screenshot_path:
                        try:
                            tg.send_photo(result.screenshot_path,
                                          caption="Unpaid hold — pay in the app")
                        except Exception:
                            pass
                return result

            _record(False, None, "no slot after retries")
            tg.send(f"🎾 {target.name} court drop {drop_local}: no slot secured "
                    f"after {attempts} attempts / {retry_window_s:.0f}s.")
            return result or RunResult(ok=False, dry_run=dry_run,
                                       message="no slot after retries")
        except Exception as e:
            log.error("drop.failed", err=str(e))
            try:
                tg.send(f"⚠️ {target.name} court drop failed — {e}")
            except Exception:
                pass
            raise
        finally:
            browser.close()


# ── entrypoint ────────────────────────────────────────────────────────────────
def run_once(target_date: str, dry_run: bool = True, headless: bool = True,
             want_time: str | None = None, target_key: str = "paddington",
             mode: str = "court", activity_label: str | None = None,
             notify: bool = True) -> RunResult:
    secrets = Secrets.from_env()
    target = load_targets()[target_key]
    tg = (Telegram(secrets.telegram_bot_token, secrets.telegram_chat_id)
          if notify else _NullTelegram())

    with sync_playwright() as p:
        browser, ctx = make_context(p, headless=headless)
        page = ctx.new_page()
        prov = EveryoneActiveProvider(secrets, target)
        try:
            prov.start_session(ctx, page)
            prov.enter_connect(page, ctx)
            if mode == "activity":
                return _run_activity(page, ctx, prov, target, target_date,
                                     dry_run, activity_label, tg)
            return _run_court(page, ctx, prov, target, target_date,
                              dry_run, want_time, tg)
        except Exception as e:
            log.error("run.failed", err=str(e))
            try:
                tg.send(f"⚠️ {target.name} {target_date}: run failed — {e}")
            except Exception:
                pass
            raise
        finally:
            browser.close()
