"""Edge-case suite for the Telegram command surface (API_SPEC §2).

The happy-path suite in `test_telegram_commands.py` walks each command once.
This file attacks the three places where a bug would actually cost something:

1. **Authorisation** (§2, PRD §7) — applied to *every* command, at both layers
   (pure handler and persisting session), including the nastiest case: a
   stranger trying to complete the owner's outstanding `/live on` handshake.
2. **The handshake** (§2.3) — a state machine with a deadline, which is where
   off-by-one and "state leaks between messages" bugs live. Tested at the
   boundary (exactly at the deadline) and across a process restart.
3. **Input parsing** — everything a thumb can type on a phone: inverted
   windows, `24:00`, empty args, mixed case, unicode, 5 kB of noise. The
   invariant under all of it is PRD §7: *the previous config stays intact*,
   asserted byte-for-byte on disk rather than field-by-field.

No network, no token, no sockets. `now` is always injected, so the two-minute
window is exercised without a single `sleep`.
"""

import datetime as dt

import pytest

from tennisbot.prefs import LONDON, Prefs, load_prefs, save_prefs
from tennisbot.telegram_commands import (CONFIRM_TIMEOUT, HELP, CommandSession,
                                         handle_message)

OWNER = "12345"
CENTRES = ["paddington", "westway"]
NOW = dt.datetime(2026, 7, 24, 10, 0, tzinfo=LONDON)
TICK = dt.timedelta(microseconds=1)

# One of every command shape, used to prove the rules below hold for ALL of
# them and not just the one the author happened to test.
EVERY_COMMAND = [
    "/status", "/help", "/start", "/centres westway", "/days Tue",
    "/window 18:00-22:00", "/length 2", "/cap 0", "/live off", "/live on",
    "CONFIRM", "hello", "/unknown",
]


def send(text, prefs=None, chat_id=OWNER, now=NOW, pending=None, **kw):
    return handle_message(text, chat_id, prefs or Prefs.defaults(),
                          owner_chat_id=OWNER, now=now,
                          pending_confirm_until=pending,
                          valid_centres=CENTRES, **kw)


def session(tmp_path, **kw):
    return CommandSession(OWNER, tmp_path, valid_centres=CENTRES, **kw)


# ---------------------------------------------------------------------------
# 1. AUTHORISATION — every command, both layers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", EVERY_COMMAND)
def test_no_command_at_all_is_honoured_from_a_foreign_chat(text):
    # PRD §7 "a message from a chat id other than TELEGRAM_CHAT_ID changes
    # nothing", applied exhaustively. Testing only /cap (as the original suite
    # does) would miss an auth check accidentally placed inside the setter
    # branch rather than at the top of the handler.
    r = send(text, chat_id="99999")
    assert r.prefs is None
    assert r.reply is None          # silence, not a refusal (§2)
    assert r.ok is False


@pytest.mark.parametrize("text", EVERY_COMMAND)
def test_no_command_at_all_reaches_the_store_from_a_foreign_chat(tmp_path,
                                                                 text):
    # Same rule through the persisting seam, asserted on the BYTES of the file:
    # stronger than comparing fields, because it also catches a spurious
    # re-stamp of `updated_at` (a write that "changed nothing" but still wrote).
    save_prefs(Prefs(weekly_cap=3, days=("Tue",)), tmp_path)
    before = (tmp_path / "prefs.json").read_bytes()
    assert session(tmp_path).handle(text, "99999", now=NOW) is None
    assert (tmp_path / "prefs.json").read_bytes() == before


def test_a_stranger_cannot_complete_the_owners_live_handshake(tmp_path):
    # THE attack that matters: the owner arms `/live on`, and in that 2-minute
    # window a stranger sends CONFIRM. Authorisation is checked before the
    # handshake, so the stranger is invisible to the state machine — they
    # neither complete it nor cancel it.
    s = session(tmp_path)
    s.handle("/live on", OWNER, now=NOW)
    armed_until = s._pending_confirm_until
    assert armed_until is not None

    assert s.handle("CONFIRM", "66666", now=NOW + dt.timedelta(seconds=5)) is None
    assert load_prefs(tmp_path).live is False           # stranger changed nothing
    assert s._pending_confirm_until == armed_until      # ...and cancelled nothing

    s.handle("CONFIRM", OWNER, now=NOW + dt.timedelta(seconds=10))
    assert load_prefs(tmp_path).live is True            # the owner still can


def test_a_stranger_cannot_cancel_the_owners_pending_handshake():
    # The mirror-image denial of service: if a foreign message fell through to
    # the "any other message cancels" rule, a stranger could grief the owner
    # out of ever enabling live mode.
    armed = send("/live on")
    foreign = send("nonsense", chat_id="99999",
                   pending=armed.pending_confirm_until)
    assert foreign.pending_confirm_until == armed.pending_confirm_until


@pytest.mark.parametrize("sender, owner, authorised", [
    (12345, "12345", True),      # Telegram sends an int; env gives a string
    ("12345", 12345, True),
    (-1001234, "-1001234", True),  # group ids are negative
    ("12345 ", "12345", False),  # whitespace is NOT trimmed away: no fuzzy match
    ("012345", "12345", False),
    ("1234", "12345", False),    # prefix must not match
    ("123456", "12345", False),
])
def test_chat_id_comparison_is_string_exact_across_types(sender, owner,
                                                         authorised):
    # The env var arrives as a string, the Telegram payload as an int, so the
    # handler compares str(...) on both sides. That coercion must not become
    # a *loose* match — hence the near-miss ids above.
    r = handle_message("/cap 1", sender, Prefs.defaults(), owner_chat_id=owner,
                       now=NOW, valid_centres=CENTRES)
    assert (r.prefs is not None) is authorised


@pytest.mark.parametrize("owner", [None, "", "   "])
def test_a_misconfigured_owner_id_authorises_nobody_real(owner):
    # If TELEGRAM_CHAT_ID is missing the bot must fail CLOSED — an unconfigured
    # deployment that accepts commands from anyone would be the worst possible
    # default. (A real Telegram update always carries a numeric chat id.)
    r = handle_message("/cap 99", 987654321, Prefs.defaults(),
                       owner_chat_id=owner, now=NOW, valid_centres=CENTRES)
    assert r.prefs is None and r.reply is None


# ---------------------------------------------------------------------------
# 2. THE LIVE HANDSHAKE (§2.3)
# ---------------------------------------------------------------------------

def test_confirm_exactly_on_the_deadline_is_still_accepted():
    # Boundary-value analysis on the timeout: the code says `now > deadline` is
    # expired, so the deadline instant itself must still work. Pinning which
    # side of the boundary is inclusive stops a later refactor flipping it.
    armed = send("/live on")
    r = send("CONFIRM", now=armed.pending_confirm_until,
             pending=armed.pending_confirm_until)
    assert r.prefs is not None and r.prefs.live is True


def test_confirm_one_microsecond_late_is_rejected():
    armed = send("/live on")
    r = send("CONFIRM", now=armed.pending_confirm_until + TICK,
             pending=armed.pending_confirm_until)
    assert r.prefs is None and "expired" in r.reply
    assert r.pending_confirm_until is None


def test_the_armed_deadline_is_exactly_two_minutes_from_now():
    assert send("/live on").pending_confirm_until == NOW + CONFIRM_TIMEOUT
    assert CONFIRM_TIMEOUT == dt.timedelta(minutes=2)   # matches §2.3 wording


def test_the_default_clock_produces_a_timezone_aware_deadline():
    # The deadline is compared against `now` on the next message. If one side
    # were naive Python raises TypeError, so the module's own default must be
    # aware — a trap for whoever wires the long-poll loop (see report).
    r = handle_message("/live on", OWNER, Prefs.defaults(), owner_chat_id=OWNER,
                       valid_centres=CENTRES)
    assert r.pending_confirm_until.tzinfo is not None


@pytest.mark.parametrize("word", ["CONFIRM", "confirm", "Confirm", " confirm ",
                                  "\nCONFIRM\n"])
def test_confirm_is_forgiving_about_case_and_whitespace(word):
    armed = send("/live on")
    assert send(word, pending=armed.pending_confirm_until).prefs.live is True


@pytest.mark.parametrize("word", ["confirm please", "CONFIRM!", "/confirm",
                                  "yes", "y", "ok", "CONFIRMED", "CONFIR"])
def test_near_miss_confirmations_do_not_enable_live(word):
    # Deliberately strict in this direction: anything that isn't exactly the
    # word cancels rather than enables. A fuzzy match here would erode the
    # entire point of the speed bump (ARCHITECTURE §8.8).
    armed = send("/live on")
    r = send(word, pending=armed.pending_confirm_until)
    assert r.prefs is None or r.prefs.live is False
    assert r.pending_confirm_until is None       # ...and it cancelled the arming


def test_live_off_while_armed_disarms_and_stays_dry_run():
    armed = send("/live on")
    r = send("/live off", pending=armed.pending_confirm_until)
    assert r.prefs.live is False
    assert r.pending_confirm_until is None
    assert "cancelled" in r.reply and "DRY-RUN" in r.reply


def test_a_rejected_command_while_armed_still_cancels_the_arming():
    # The rejection path returns early (it raises PrefsError before dispatch
    # finishes), so it is a plausible place to forget to clear the handshake —
    # which would leave a CONFIRM armed long after the owner moved on.
    armed = send("/live on")
    r = send("/cap minus one", pending=armed.pending_confirm_until)
    assert r.prefs is None
    assert r.pending_confirm_until is None
    assert send("CONFIRM", pending=r.pending_confirm_until).prefs is None


def test_re_arming_extends_the_deadline_rather_than_keeping_the_old_one():
    armed = send("/live on")
    later = NOW + dt.timedelta(seconds=90)
    again = send("/live on", pending=armed.pending_confirm_until, now=later)
    assert again.pending_confirm_until == later + CONFIRM_TIMEOUT
    # ...and the OLD deadline is genuinely gone: confirming at the old expiry
    # must now be judged against the new one.
    ok = send("CONFIRM", now=later + dt.timedelta(seconds=10),
              pending=again.pending_confirm_until)
    assert ok.prefs.live is True


def test_an_expired_arming_followed_by_live_on_re_arms_cleanly():
    armed = send("/live on")
    much_later = NOW + dt.timedelta(minutes=10)
    r = send("/live on", pending=armed.pending_confirm_until, now=much_later)
    assert "expired" in r.reply
    assert r.pending_confirm_until == much_later + CONFIRM_TIMEOUT


def test_the_handshake_does_not_survive_a_process_restart(tmp_path):
    # The arming lives in memory on purpose (CommandSession._pending...), so a
    # catcher restart mid-handshake must lose it. Fail-safe direction: the
    # worst case is the owner re-types /live on, never an accidental live flip.
    s1 = session(tmp_path)
    s1.handle("/live on", OWNER, now=NOW)
    s2 = session(tmp_path)                      # the restart
    s2.handle("CONFIRM", OWNER, now=NOW + dt.timedelta(seconds=5))
    assert load_prefs(tmp_path).live is False


def test_confirm_with_nothing_armed_never_writes(tmp_path):
    s = session(tmp_path)
    assert "Nothing to confirm" in s.handle("CONFIRM", OWNER, now=NOW)
    assert load_prefs(tmp_path).live is False
    assert not (tmp_path / "prefs.json").exists()   # a read-only path wrote nothing


def test_live_off_is_immediate_and_needs_no_confirmation(tmp_path):
    # Asymmetry by design (§2.3): the *safe* direction has no speed bump, so a
    # panicking owner can always stop real bookings in one message.
    save_prefs(Prefs(live=True), tmp_path)
    s = session(tmp_path)
    reply = s.handle("/live off", OWNER, now=NOW)
    assert "DRY-RUN" in reply
    assert load_prefs(tmp_path).live is False


def test_the_confirmed_live_flip_is_stamped_as_telegram_provenance(tmp_path):
    s = session(tmp_path)
    s.handle("/live on", OWNER, now=NOW)
    s.handle("CONFIRM", OWNER, now=NOW + dt.timedelta(seconds=1))
    on_disk = load_prefs(tmp_path)
    assert on_disk.live is True
    assert on_disk.updated_by == "telegram" and on_disk.updated_at


# ---------------------------------------------------------------------------
# 3. PARSING & VALIDATION — the invariant is "previous config intact"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec, earliest, latest", [
    ("18:00-22:00", "18:00", "22:00"),
    ("18:00-", "18:00", None),
    ("-22:00", None, "22:00"),
    ("00:00-23:59", "00:00", "23:59"),      # widest legal window
    ("18:00 - 22:00", "18:00", "22:00"),    # spaces around the dash
    ("  18:00-22:00  ", "18:00", "22:00"),
    ("any", None, None),
    ("ANY", None, None),
    ("all", None, None),
    ("*", None, None),
])
def test_window_accepts_every_documented_form(spec, earliest, latest):
    r = send(f"/window {spec}")
    assert (r.prefs.earliest, r.prefs.latest) == (earliest, latest)


@pytest.mark.parametrize("spec", [
    "24:00-", "-24:00", "18:00-24:00",   # 24h clock ends at 23:59
    "18:00-18:00",                       # zero-width window: nothing bookable
    "22:00-18:00",                       # inverted
    "23:59-00:00",                       # would wrap midnight
    "18:00",                             # no separator
    "1800-2200", "6pm-10pm", "18.00-22.00",
    "18:00-22:00-23:00",
    "any-18:00",
    "18:00–22:00",                       # EN DASH: what the bot itself prints
    "",                                  # no argument at all
    "🎾",
])
def test_window_rejects_and_leaves_the_config_intact(spec):
    # PRD §7 in its sharpest form: for EVERY malformed window, no candidate
    # config comes back, so the caller has nothing to save. The intactness is
    # structural (prefs is None), not something the handler has to remember.
    before = Prefs(earliest="18:00", latest="22:00")
    r = send(f"/window {spec}".strip(), before)
    assert r.prefs is None and r.ok is False
    assert "18:00" in r.reply              # tells the owner what still applies
    assert before.summary() in r.reply


def test_the_en_dash_the_bot_prints_is_not_a_dash_it_can_read():
    # Usability finding, pinned as behaviour: `window_text()` renders
    # "18:00–22:00" with an EN DASH, so copy-pasting the bot's own summary back
    # as a command is rejected. Safe (nothing changes) but confusing — logged
    # in the report as a UX nit, not a correctness bug.
    assert "–" in Prefs(earliest="18:00", latest="22:00").window_text()
    assert send("/window 18:00–22:00").prefs is None


@pytest.mark.parametrize("arg, expected", [
    ("0", 0), ("1", 1), ("3", 3), ("007", 7), ("+3", 3), ("99", 99),
])
def test_cap_accepts_whole_numbers_including_zero(arg, expected):
    assert send(f"/cap {arg}").prefs.weekly_cap == expected


@pytest.mark.parametrize("arg", ["-1", "3.5", "three", "", "1 2", "1_0",
                                 "0x3", "٣٣٣٣٣٣٣٣٣", "nan", "inf", "-0", "½"])
def test_cap_rejects_anything_that_is_not_a_whole_number(arg):
    if arg == "٣٣٣٣٣٣٣٣٣":
        # Arabic-Indic digits ARE digits to Python and int() understands them,
        # so this is accepted. Harmless, recorded so the behaviour is known.
        assert send(f"/cap {arg}").prefs.weekly_cap == 333333333
        return
    r = send(f"/cap {arg}".strip())
    assert r.prefs is None and r.ok is False


@pytest.mark.parametrize("arg", ["²", "⁵"])
def test_cap_with_a_superscript_digit_is_rejected_not_crashed(arg):
    r = send(f"/cap {arg}")
    assert r.prefs is None and r.ok is False


@pytest.mark.parametrize("arg, expected", [("1", 1), ("2", 2)])
def test_length_accepts_one_and_two(arg, expected):
    assert send(f"/length {arg}").prefs.slot_length_hours == expected


@pytest.mark.parametrize("arg", ["0", "3", "1.0", "01", "one", "", "1 2",
                                 "-1", "²", "24"])
def test_length_rejects_everything_else(arg):
    # `/length` compares the raw string against ("1", "2") rather than int()ing
    # it — which is why the superscript that crashes /cap is harmless here. A
    # good accidental lesson in "validate before you convert".
    r = send(f"/length {arg}".strip())
    assert r.prefs is None and r.ok is False


@pytest.mark.parametrize("arg, expected", [
    ("Tue Thu", ("Tue", "Thu")),
    ("tue,thu", ("Tue", "Thu")),
    ("THU,TUE", ("Tue", "Thu")),
    ("Tuesday thursday", ("Tue", "Thu")),
    ("sun mon", ("Mon", "Sun")),
    ("Tue Tue Tue", ("Tue",)),
    ("Mon Tue Wed Thu Fri Sat Sun", ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat",
                                     "Sun")),
    ("any", ()), ("all", ()), ("*", ()),
])
def test_days_parsing(arg, expected):
    assert send(f"/days {arg}").prefs.days == expected


@pytest.mark.parametrize("arg", ["Funday", "Tue Funday", "any Tue", "Mardi",
                                 "T", "Tue2", "🎾", "", "Tues day"])
def test_days_rejects_unknown_tokens_and_keeps_the_old_days(arg):
    before = Prefs(days=("Tue", "Thu"))
    r = send(f"/days {arg}".strip(), before)
    assert r.prefs is None and r.ok is False
    assert "Tue" in r.reply


@pytest.mark.parametrize("arg, expected", [
    ("paddington", ("paddington",)),
    ("PADDINGTON", ("paddington",)),
    ("paddington westway", ("paddington", "westway")),
    ("paddington,westway", ("paddington", "westway")),
    ("westway paddington", ("westway", "paddington")),   # order is preserved
    ("paddington paddington", ("paddington",)),          # ...but de-duplicated
    ("  paddington  ", ("paddington",)),
])
def test_centres_parsing(arg, expected):
    assert send(f"/centres {arg}").prefs.centres == expected


@pytest.mark.parametrize("arg", ["atlantis", "paddington atlantis", ",,,", "",
                                 "🎾", "paddington2"])
def test_centres_rejects_unknown_keys_and_lists_the_valid_ones(arg):
    # A typo'd centre would otherwise be persisted and silently book nothing
    # forever — the failure mode with no error message, which is the worst kind.
    r = send(f"/centres {arg}".strip())
    assert r.prefs is None and r.ok is False
    assert "paddington" in r.reply


def test_an_unknown_centre_is_REJECTED_when_targets_cannot_be_read():
    # DECIDED 2026-07-24 (this test previously asserted the opposite, pinning
    # the old fail-open for the owner to rule on). An empty centre list means
    # "we couldn't read targets.yaml", NOT "anything goes" — skipping the
    # membership check let arbitrary text be PERSISTED into prefs.json, and
    # that text was then rendered into every future reply, breaking the only
    # observability channel until the file was hand-edited. Fail closed:
    # refuse the change and say so; the owner can retry.
    r = handle_message("/centres atlantis", OWNER, Prefs.defaults(),
                       owner_chat_id=OWNER, now=NOW, valid_centres=[])
    assert r.prefs is None, "must not persist an unvalidated centre"
    assert r.ok is False
    assert "centre list" in r.reply


@pytest.mark.parametrize("text", ["/CAP 3", "/Cap 3", "/cap@tennisbot 3",
                                  "/CAP@TennisBot 3"])
def test_command_names_are_case_insensitive_and_bot_suffixed(text):
    # Telegram appends @botname in groups, and phone keyboards autocapitalise.
    assert send(text).prefs.weekly_cap == 3


@pytest.mark.parametrize("text", ["", "   ", "\n", "\t", "\xa0", None])
def test_blank_messages_are_silently_ignored(text):
    # Telegram delivers updates with no text at all (a sticker, a photo, a
    # service message). They must be a no-op, not an "unknown command" reply
    # that would make the bot chatty about every non-text event.
    r = send(text)
    assert r.prefs is None and r.reply is None


def test_a_blank_message_does_not_disturb_an_armed_handshake():
    armed = send("/live on")
    r = send("", pending=armed.pending_confirm_until)
    assert r.pending_confirm_until == armed.pending_confirm_until


@pytest.mark.parametrize("text", ["x" * 5000, "/cap " + "9" * 500,
                                  "/days " + "Tue " * 500,
                                  "/centres " + "westway " * 500,
                                  "/" + "a" * 4096, "/window " + "-" * 1000])
def test_very_long_input_neither_crashes_nor_hangs(text):
    # No length limit exists anywhere in the handler; this asserts the absence
    # of a limit is *safe* (bounded work, a sane reply) rather than just untested.
    r = send(text)
    assert r.reply is None or isinstance(r.reply, str)


@pytest.mark.parametrize("text", ["/dance", "/status/", "//", "/", "/live!",
                                  "/cap;rm -rf", "hello", "🎾", "/help me"])
def test_junk_commands_get_a_pointer_and_change_nothing(text):
    r = send(text)
    assert r.prefs is None
    assert r.reply is not None            # the owner always gets *something*


def test_start_is_an_alias_for_help():
    # /start is what Telegram sends the first time a chat is opened; without an
    # alias the very first interaction with the bot would be "unknown command".
    assert send("/start").reply == HELP


@pytest.mark.parametrize("text", ["/centres <b>oops", "/days <i", "/window <b-"])
def test_user_text_echoed_back_is_html_escaped(text):
    r = send(text)
    assert "<b" not in r.reply and "<i" not in r.reply


def test_the_confirm_window_is_two_real_minutes_even_across_a_dst_fold():
    base = dt.datetime(2027, 10, 31, 1, 59, tzinfo=LONDON)   # BST→GMT that night
    deadline = send("/live on", now=base).pending_confirm_until
    elapsed = (deadline.astimezone(dt.timezone.utc)
               - base.astimezone(dt.timezone.utc))
    assert elapsed == CONFIRM_TIMEOUT


# ---------------------------------------------------------------------------
# 4. READ-BACK reflects what is ACTUALLY active (PRD §7)
# ---------------------------------------------------------------------------

def test_status_reads_through_to_disk_on_every_call(tmp_path):
    # "the read-back command returns the currently-active settings" — so the
    # session must not cache. We change the file underneath a live session
    # (as a redeploy or a hand-edit would) and expect the next /status to move.
    s = session(tmp_path)
    save_prefs(Prefs(weekly_cap=1), tmp_path)
    assert "1" in s.handle("/status", OWNER, now=NOW)
    save_prefs(Prefs(weekly_cap=7), tmp_path)
    assert "7" in s.handle("/status", OWNER, now=NOW)


def test_status_on_a_corrupt_store_shows_the_defaults_and_says_dry_run(tmp_path):
    # The two modules meeting: a corrupt file must still produce a sane
    # read-back (defaults, DRY-RUN) rather than an exception in the poll loop.
    (tmp_path / "prefs.json").write_text("{{{corrupt")
    reply = session(tmp_path).handle("/status", OWNER, now=NOW)
    assert "DRY-RUN" in reply and "paddington" in reply
    assert "never" in reply           # updated_at unknown ⇒ "never", not None


def test_status_renders_unknown_for_facts_the_catcher_has_not_injected():
    # API_SPEC §2.1's flagged gap: paid-count and next-scan come from the
    # catcher. Until it exists they must render as "unknown" — visibly missing,
    # not silently zero (which would read as "0 booked" and be wrong).
    reply = send("/status").reply
    assert "Next scan: unknown" in reply
    assert "Weekly cap: 3" in reply and "0/3" not in reply


def test_status_shows_the_paid_count_against_the_cap_when_injected():
    assert "2/3 paid this week" in send("/status", paid_this_week=2).reply


def test_status_never_writes(tmp_path):
    session(tmp_path).handle("/status", OWNER, now=NOW)
    assert not (tmp_path / "prefs.json").exists()


@pytest.mark.parametrize("cmd, needle", [
    ("/days Tue Thu", "Tue,Thu"),
    ("/window 18:00-22:00", "18:00–22:00"),
    ("/length 2", "2h"),
    ("/cap 0", "cap 0"),
    ("/centres westway", "westway"),
])
def test_every_setter_echoes_the_whole_active_config(cmd, needle):
    # §2.2: "each replies with the new value AND a one-line summary of the full
    # active config" — so the phone always shows the whole picture, including
    # the mode. Checked for every setter, not just the one in the happy path.
    r = send(cmd)
    assert needle in r.reply
    assert r.prefs.summary() in r.reply
    assert r.reply.count("\n") >= 1


def test_a_setter_never_disturbs_the_other_fields(tmp_path):
    # Field isolation through the persisting seam: `dataclasses.replace` should
    # copy everything else forward. A dict-merge bug here would quietly reset
    # the owner's other preferences on every command.
    s = session(tmp_path)
    s.handle("/days Tue Thu", OWNER, now=NOW)
    s.handle("/window 18:00-22:00", OWNER, now=NOW)
    s.handle("/length 2", OWNER, now=NOW)
    s.handle("/cap 1", OWNER, now=NOW)
    s.handle("/centres westway", OWNER, now=NOW)
    p = load_prefs(tmp_path)
    assert (p.days, p.earliest, p.latest, p.slot_length_hours, p.weekly_cap,
            p.centres) == (("Tue", "Thu"), "18:00", "22:00", 2, 1, ("westway",))
    assert p.live is False           # ...and never an accidental live flip


@pytest.mark.parametrize("bad", ["/cap -1", "/length 5", "/window 25:00-",
                                 "/days Funday", "/centres atlantis",
                                 "/live maybe", "/window 22:00-18:00"])
def test_no_rejected_command_ever_touches_the_file(tmp_path, bad):
    # The acceptance criterion, asserted on bytes across every rejecting
    # command: not one of them may rewrite prefs.json — not even to re-stamp
    # `updated_at`, which would make the audit trail lie.
    s = session(tmp_path)
    s.handle("/days Tue", OWNER, now=NOW)
    before = (tmp_path / "prefs.json").read_bytes()
    reply = s.handle(bad, OWNER, now=NOW)
    assert reply.startswith("⚠️")
    assert "Unchanged" in reply or "Usage" in reply
    assert (tmp_path / "prefs.json").read_bytes() == before


def test_settings_survive_a_restart_including_the_live_flag(tmp_path):
    # PRD §7 end to end, with the field that matters most: a restarted process
    # must come back LIVE if the owner set it LIVE (and dry-run otherwise).
    s1 = session(tmp_path)
    s1.handle("/cap 1", OWNER, now=NOW)
    s1.handle("/live on", OWNER, now=NOW)
    s1.handle("CONFIRM", OWNER, now=NOW + dt.timedelta(seconds=5))
    reply = session(tmp_path).handle("/status", OWNER, now=NOW)
    assert reply.splitlines()[0].endswith("<b>LIVE</b>")
    assert "Weekly cap: 1" in reply
