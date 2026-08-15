"""Nag dialogs that steal the keyboard.

On 2026-08-15 an operator run typed a six-digit 2FA code into nothing for
sixty steps. Five modal dialogs were stacked behind Chrome — two Ubuntu crash
reporters, "Google Chrome has closed unexpectedly", a release-upgrade prompt
and Software Updater. They held keyboard focus, so pointer clicks still landed
on the page and only the keystrokes went nowhere. Nothing in the screenshot
looked wrong, and the model had no way to tell.
"""
import asyncio

import pytest

from director.operator import display as display_mod


class FakeRun:
    """Stands in for wmctrl: a window list, and a record of what was closed."""

    def __init__(self, listing, fail=False):
        self.listing = listing
        self.fail = fail
        self.closed = []

    async def __call__(self, argv, timeout=20.0, env=None):
        if argv[:2] == ["wmctrl", "-l"]:
            return (1, "") if self.fail else (0, self.listing)
        if argv[:2] == ["wmctrl", "-ic"]:
            self.closed.append(argv[2])
            return 0, ""
        return 0, ""


LISTING = "\n".join([
    "0x00600003  0 calle-linux Software Updater",
    "0x03400003  0 calle-linux Ubuntu 24.04.4 LTS Upgrade Available",
    "0x02c00004  0 calle-linux DistroKid - Google Chrome",
    "0x01000001  0 calle-linux Sorry, Ubuntu 22.04 has experienced an internal error",
    "0x01000002  0 calle-linux The application Google Chrome has closed unexpectedly",
    "0x03e00003 -1 calle-linux @!0,0;BDHF",
])


def test_the_nags_are_found_and_the_real_window_is_not(monkeypatch):
    monkeypatch.setattr(display_mod, "_run", FakeRun(LISTING))
    found = asyncio.run(display_mod.stray_dialogs({}))
    assert len(found) == 4
    blob = " ".join(found).casefold()
    assert "software updater" in blob
    assert "upgrade available" in blob
    assert "internal error" in blob
    assert "closed unexpectedly" in blob
    assert "distrokid" not in blob, "the browser is the task, not a nag"


def test_closing_them_leaves_the_browser_alone(monkeypatch):
    fake = FakeRun(LISTING)
    monkeypatch.setattr(display_mod, "_run", fake)
    closed = asyncio.run(display_mod.dismiss_stray_dialogs({}))
    assert len(closed) == 4
    assert "0x02c00004" not in fake.closed, "that is the page being worked on"
    assert set(fake.closed) == {"0x00600003", "0x03400003", "0x01000001", "0x01000002"}


def test_a_clean_desktop_closes_nothing(monkeypatch):
    fake = FakeRun("0x02c00004  0 calle-linux DistroKid - Google Chrome")
    monkeypatch.setattr(display_mod, "_run", fake)
    assert asyncio.run(display_mod.dismiss_stray_dialogs({})) == []
    assert fake.closed == []


def test_no_window_manager_is_not_an_error(monkeypatch):
    """wmctrl is missing on a dev box; the run must still start."""
    monkeypatch.setattr(display_mod, "_run", FakeRun("", fail=True))
    assert asyncio.run(display_mod.stray_dialogs({})) == []


def test_a_title_with_spaces_survives_the_parse(monkeypatch):
    fake = FakeRun("0x00600003  0 calle-linux Software Updater")
    monkeypatch.setattr(display_mod, "_run", fake)
    assert asyncio.run(display_mod.stray_dialogs({})) == ["0x00600003 Software Updater"]


def test_chrome_is_launched_without_the_bubble_that_eats_the_keyboard():
    """`--disable-session-crashed-bubble` covers the old infobar only; current
    Chrome shows a "Restore pages?" bubble that takes keyboard focus."""
    argv = display_mod.chrome_argv("", {}) if hasattr(display_mod, "chrome_argv") else None
    if argv is None:  # the builder is private; read the source instead
        import inspect
        argv = inspect.getsource(display_mod)
    blob = " ".join(argv) if isinstance(argv, list) else argv
    assert "--hide-crash-restore-bubble" in blob
    assert "--disable-session-crashed-bubble" in blob


def test_the_run_clears_them_before_it_starts():
    import inspect

    from director.operator import loop

    source = inspect.getsource(loop.run_task)
    assert "dismiss_stray_dialogs" in source
    before = source.index("dismiss_stray_dialogs")
    assert before < source.index("while True:"), "must happen before the first step"


def test_an_unchanged_screen_looks_for_a_dialog_that_appeared_mid_run():
    import inspect

    from director.operator import loop

    source = inspect.getsource(loop.run_task)
    assert source.count("dismiss_stray_dialogs") >= 2
    assert "did not reach the" in source, "the model must be told to retype"
