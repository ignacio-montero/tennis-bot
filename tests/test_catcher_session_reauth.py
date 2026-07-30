"""Regression: the catcher must re-authenticate a lapsed Connect session.

The daemon logs in once and then only navigates each cycle. The Connect cookie
expires after a few hours, so a one-shot session silently bounced every scan to
MRMLogin for 3 days (2026-07-27→30, `go_home landed off search page`). The fix
re-affirms the session each reused cycle and heals it via `enter_connect`'s
MRMLogin path (NOT the flaky account SPA). These tests exercise the real
`_PlaywrightScanner._ensure_session` branch logic without booting Chromium.
"""

from tennisbot.catcher import _PlaywrightScanner


def _bare_scanner():
    """A scanner with the browser already 'built', bypassing the real __init__
    (no Chromium, no env). Non-None `_p`/`_browser` make `_ensure_session` take
    the reused-session branch rather than trying to launch a browser."""
    s = object.__new__(_PlaywrightScanner)
    s._p = object()
    s._browser = object()
    s._ctx = object()
    s._page = object()
    s._session_ready = True
    return s


def test_reused_session_reauths_when_connect_dead(monkeypatch):
    s = _bare_scanner()
    calls = []
    monkeypatch.setattr(s, "_connect_live", lambda: False)
    monkeypatch.setattr(s, "_establish_session",
                        lambda *, full: calls.append(full))
    s._ensure_session()
    # Lapsed session → re-auth via the heal path (full=False: no account SPA).
    assert calls == [False]


def test_reused_session_skips_reauth_when_alive(monkeypatch):
    s = _bare_scanner()
    calls = []
    monkeypatch.setattr(s, "_connect_live", lambda: True)
    monkeypatch.setattr(s, "_establish_session",
                        lambda *, full: calls.append(full))
    s._ensure_session()
    # Healthy session → no redundant login (stay polite; don't hammer EA).
    assert calls == []


def test_first_time_uses_full_establishment(monkeypatch):
    s = _bare_scanner()
    s._session_ready = False
    calls = []

    def _no_probe():
        raise AssertionError("must not probe liveness before first login")

    monkeypatch.setattr(s, "_connect_live", _no_probe)
    monkeypatch.setattr(s, "_establish_session",
                        lambda *, full: calls.append(full))
    s._ensure_session()
    # First establishment seeds account-level cookies (full=True).
    assert calls == [True]
