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
from .prefs import load_prefs
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
    # Diagnosis carriers (see RunResult): getting INTO a grid at all is what
    # distinguishes "the drop hasn't happened" from "it happened and I lost".
    grid_seen = False
    n_avail_max = 0
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
        # We reached a real timetable => the row was NOT Full => released.
        grid_seen = True
        n_avail_max = max(n_avail_max, len(avail_times))
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
                             chosen=chosen[0], screenshot_path=shot,
                             grid_seen=grid_seen, n_avail=n_avail_max)

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
                         chosen=chosen[0], screenshot_path=hold_shot,
                         grid_seen=grid_seen, n_avail=n_avail_max)

    tg.send(f"🎾 {target.name} {target_date}: nothing booked.\n"
            f"{'; '.join(notes) or 'no surfaces enabled'}")
    return RunResult(ok=True, dry_run=dry_run, message="no preferred slot; "
                     + "; ".join(notes),
                     grid_seen=grid_seen, n_avail=n_avail_max)


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


def diagnose_drop_failure(grid_seen: bool, n_avail: int,
                          after_time: str | None = None,
                          want_time: str | None = None) -> tuple[str, str]:
    """Why did a drop secure nothing? Returns (code, human-readable reason).

    A single read can't tell us: `row_full` means BOTH "not released yet" and
    "released but sold out" (see CLAUDE.md). What disambiguates is whether we
    ever got INTO a timetable across the whole camp window — a row that stays
    Full for ~90s of retries did not open at all, whereas a grid we could read
    means the drop happened and we simply didn't secure anything from it.

    - `never_opened`      → the release did NOT happen at this instant. This is
                            the signal that the drop time may have moved, and
                            the one that warrants re-running the watcher.
    - `sold_out`          → released, but every slot was gone by the time we
                            parsed the grid. We lost the race.
    - `prefs_too_narrow`  → released, slots WERE free, but none matched the
                            filter. Not a race loss — a configuration problem.
    """
    if not grid_seen:
        return ("never_opened",
                "row stayed Full for the whole window — the courts never "
                "opened. The drop time may have moved; re-run the watcher.")
    if n_avail == 0:
        return ("sold_out",
                "grid opened but 0 slots were free — everything was taken "
                "before we could parse it. Lost the race.")
    filt = (f"after {after_time}" if after_time
            else f"want {want_time}" if want_time else "ranked prefs")
    return ("prefs_too_narrow",
            f"grid opened with {n_avail} free slot(s), but none matched "
            f"({filt}). Widen the filter, not the timing.")


# ── timed court-drop entrypoint ────────────────────────────────────────────────
def run_drop(target_key: str = "paddington", dry_run: bool = True,
             headless: bool = True, want_time: str | None = None,
             time_override: str | None = None, notify: bool = True,
             epsilon: float = 0.15, retry_window_s: float = 90.0,
             retry_gap_s: float = 1.0,
             after_time: str | None = None,
             prewarm_attempts: int = 3,
             prewarm_floor_s: float = 25.0,
             two_hours: bool | None = None) -> RunResult:
    """Pre-warm a session, spin-wait to the server-clock drop instant, then
    'camp' the booking: retry rapidly through overload / not-yet-dropped until it
    books or `retry_window_s` elapses. Targets the date the drop releases
    (drop date + days_before — see _next_drop for the midnight-rollover reason).

    `after_time` (HH:MM) books the earliest available court at/after that time,
    any court — used by the dry-run rehearsal ('book any court after 19:00').

    `prewarm_attempts` / `prewarm_floor_s` bound the cold-login retries: keep
    trying to arm until we succeed, run out of attempts, or get within
    `prewarm_floor_s` of the instant (never still be logging in at the drop).

    `two_hours` (None = leave the configured value untouched; the loop passes
    True when `prefs.slot_length_hours == 2`) toggles the target's two-consecutive-
    hours mode WITHOUT modifying the booking engine — it reuses the catcher's
    behaviour-preserving `dataclasses.replace` trick (`_with_two_hours`). None is
    the default so a direct `run_drop`/`drop` call behaves exactly as before.
    """
    import time as _time
    secrets = Secrets.from_env()
    target = load_targets()[target_key]
    if two_hours is not None:
        # Reuse the catcher's helper so the drop and the catcher feed the engine
        # the same replace()-copied target; lazy import avoids a runner<->catcher
        # import cycle (catcher imports runner inside its functions).
        from .catcher import _with_two_hours
        target = _with_two_hours(target, two_hours)
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
            # Pre-warm (authenticate + enter Connect + measure skew), WITH
            # RETRIES. The sidecar's session sits unused ~24h between nights, so
            # this is always a cold login — and on 2026-07-23 a single 45s
            # navigation timeout burned the only attempt and cost the whole
            # night (53 slots were released; we booked nothing). Retrying inside
            # the lead window is what turns `lead_min` runway into resilience.
            skew = None
            attempt = 0                      # defined even if attempts <= 0
            last_err: Exception | None = None
            for attempt in range(1, prewarm_attempts + 1):
                try:
                    prov.start_session(ctx, page)
                    prov.enter_connect(page, ctx)
                    skew = clock.server_skew()
                    log.info("drop.armed", date=target_date, drop_local=drop_local,
                             skew=round(skew, 3), after_time=after_time,
                             attempt=attempt)
                    break
                except Exception as e:
                    last_err = e
                    left = instant - _time.time()
                    log.warn("drop.prewarm_failed", attempt=attempt,
                             s_to_drop=round(left), err=str(e)[:200])
                    # Stop if we're out of attempts or too close to the instant
                    # to finish another login — better to fail loud early than
                    # to still be logging in when the courts drop.
                    if attempt >= prewarm_attempts or left <= prewarm_floor_s:
                        break
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = ctx.new_page()      # fresh page; the old one may be wedged
            if skew is None:
                raise RuntimeError(
                    f"pre-warm failed after {attempt} attempt(s): {last_err}")

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
            # Accumulate across the WHOLE camp window, not just the last read:
            # one attempt landing mid-postback shouldn't decide the diagnosis.
            ever_grid_seen = False
            best_avail = 0
            while _time.time() < deadline:
                attempts += 1
                try:
                    result = _run_court(page, ctx, prov, target, target_date,
                                        dry_run, want_time, quiet, after_time)
                except Exception as e:                       # overload / timeout
                    log.warn("drop.attempt_failed", n=attempts, err=str(e)[:120])
                    _time.sleep(retry_gap_s)
                    continue
                ever_grid_seen = ever_grid_seen or result.grid_seen
                best_avail = max(best_avail, result.n_avail)
                if result.chosen is not None:                # secured / would-book
                    log.info("drop.secured", attempts=attempts)
                    break
                _time.sleep(retry_gap_s)                      # not up yet — retry

            def _record(secured, chosen_slot, msg, reason_code=None):
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
                    # Diagnosis, so a run of bad nights is queryable from the
                    # jsonl: all `never_opened` = the drop moved; all
                    # `sold_out` = we're just too slow.
                    "grid_seen": ever_grid_seen, "n_avail": best_avail,
                    "reason_code": reason_code,
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

            code, reason = diagnose_drop_failure(ever_grid_seen, best_avail,
                                                 after_time, want_time)
            log.info("drop.diagnosis", code=code, grid_seen=ever_grid_seen,
                     n_avail=best_avail, reason=reason)
            _record(False, None, f"no slot after retries — {reason}",
                    reason_code=code)
            tg.send(f"🎾 {target.name} court drop {drop_local}: no slot secured "
                    f"after {attempts} attempts / {retry_window_s:.0f}s.\n"
                    f"<b>Why:</b> {reason}")
            return result or RunResult(ok=False, dry_run=dry_run,
                                       message="no slot after retries")
        except Exception as e:
            log.error("drop.failed", err=str(e))
            # A crash before the camp loop previously wrote NO outcome at all
            # (2026-07-23), so the most diagnostic failures were invisible in
            # drop-outcomes.jsonl. `no_observation` is deliberately distinct from
            # `never_opened`: we did not observe a closed grid, we failed to
            # observe anything — so it says nothing about the drop time and must
            # never be read as "the drop moved".
            try:
                _append_drop_outcome({
                    "ts": dt.datetime.now(ZoneInfo(target.drop.timezone)).isoformat(),
                    "target": target.key, "target_date": target_date,
                    "drop_local": drop_local, "after_time": after_time,
                    "dry_run": dry_run, "secured": False, "chosen": None,
                    "grid_seen": False, "n_avail": 0,
                    "reason_code": "no_observation",
                    "message": f"run failed before observing the grid: {e}"[:300],
                })
            except Exception:
                pass
            try:
                tg.send(f"⚠️ {target.name} court drop failed — {e}")
            except Exception:
                pass
            raise
        finally:
            browser.close()


def run_drop_loop(target_key: str = "paddington", want_time: str | None = None,
                  after_time: str | None = None, lead_min: float = 10.0,
                  dry_run: bool = True, notify: bool = True,
                  headless: bool = True, max_iters: int | None = None,
                  epsilon: float = 0.15, retry_window_s: float = 90.0,
                  retry_gap_s: float = 1.0, config_dir=None) -> None:
    """Self-scheduling nightly drop booker — the homelab `drop` sidecar.

    One long-running container (no host cron, no Docker socket): each night it
    sleeps until `lead_min` minutes before the next drop instant, runs one
    `run_drop` (which pre-warms a session and spin-waits to the instant), and
    loops. Reuses `_next_drop` for the instant/date so the midnight-rollover
    logic is shared with `run_drop`. Exactly one attempt per night — after an
    instant is handled we schedule strictly past it, so a fast pre-warm failure
    can't hot-spin the loop (the real drop contention is handled inside
    `run_drop`'s camp/retry window, not here).

    **Shared prefs govern BOTH jobs (ARCHITECTURE §8.6).** Each night, at pre-arm
    (after the sleep, so the read is fresh — API_SPEC §1.5), it reads the same
    `prefs.json` the catcher does. This makes the sprinter *day-filtered*: the
    drop only ever releases a single weekday (D+7), so with e.g. `days:[Tue,Thu]`
    it acts only on nights whose D+7 is preferred and skips the rest — WITHOUT
    logging in (no EA session spent on an unwanted night).

    **Strict backward-compatibility.** Precedence is "a *set* prefs field wins,
    else fall back to the CLI arg": `prefs.earliest`→`after_time`,
    `prefs.slot_length_hours==2`→two-hours, live gate = `prefs.live OR --live`.
    With no `prefs.json` (or an all-default one) every field is at its permissive
    default, so day-filter never skips, `after_time`/two-hours are untouched, and
    the effective dry-run equals the CLI `--live` flag — i.e. IDENTICAL firing to
    before this change (proved by `test_drop_loop_default_prefs_identical_firing`).

    `config_dir` overrides the prefs location; None = the env-based default
    (`$TENNISBOT_CONFIG_DIR`), exactly like the catcher.
    """
    import time as _time
    d = load_targets()[target_key].drop
    handled = 0.0
    n = 0
    while max_iters is None or n < max_iters:
        n += 1
        # next drop STRICTLY after the last one handled (grace_s=0 so a
        # just-fired instant is never re-selected).
        ref = max(_time.time(), handled + 1.0)
        instant, target_date = _next_drop(d.local_time, d.timezone,
                                          d.days_before, now_epoch=ref,
                                          grace_s=0.0)
        gap = (instant - lead_min * 60.0) - _time.time()
        if gap > 0:
            log.info("drop_loop.sleep", wake_in_s=round(gap),
                     drop_date=target_date, lead_min=lead_min)
            _time.sleep(gap)

        # Pre-arm: read the shared prefs fresh (governs both jobs, §8.6). Never
        # raises — a missing/corrupt file degrades to safe defaults (§1.1/§1.4a).
        prefs = load_prefs(config_dir)

        # Day-filter (§8.6): the drop releases exactly one weekday (D+7). If that
        # weekday isn't preferred, skip tonight and reschedule to the next night
        # — crucially WITHOUT logging in, so we don't burn an EA session (or race
        # the blackout) on a night we don't want a court anyway. Empty `days`
        # ⇒ allows_date is always True ⇒ this never fires (backward-compatible).
        if not prefs.allows_date(target_date):
            log.info("drop_loop.skipped_not_preferred_day",
                     drop_date=target_date, days=list(prefs.days))
            handled = instant
            continue

        # Precedence: a SET prefs field wins; otherwise fall back to the CLI arg
        # so no/default prefs behaves exactly as today.
        eff_after = prefs.earliest if prefs.earliest is not None else after_time
        # slot_length_hours: only force two-hours ON (==2). ==1 is the default and
        # is indistinguishable from "unset", so forcing it OFF could override the
        # target's own config on a default read — which backward-compat forbids.
        eff_two_hours = True if prefs.slot_length_hours == 2 else None
        # Live gate = prefs.live OR --live (the unified Telegram-live AND a
        # deploy-level --live both work). `prefs.degraded` forces dry-run: a
        # half-read config means we can't trust the owner's intent, so refuse to
        # book live even if --live is set (§1.4a "refuse to book").
        live = (prefs.live or (not dry_run)) and not prefs.degraded

        log.info("drop_loop.fire", drop_date=target_date, dry_run=not live,
                 mode=("LIVE" if live else "DRY-RUN"), after_time=eff_after,
                 two_hours=bool(eff_two_hours), degraded=list(prefs.degraded))
        try:
            run_drop(target_key=target_key, dry_run=not live, headless=headless,
                     want_time=want_time, after_time=eff_after,
                     two_hours=eff_two_hours, notify=notify, epsilon=epsilon,
                     retry_window_s=retry_window_s, retry_gap_s=retry_gap_s)
        except Exception as e:                       # never let one night kill the loop
            log.error("drop_loop.iter_failed", err=str(e))
        handled = instant


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
