import pytest

import helper_overlay


class FakeRoot:
    def __init__(self):
        self.next_id = 0
        self.jobs = {}

    def after(self, _delay, callback):
        self.next_id += 1
        self.jobs[self.next_id] = callback
        return self.next_id

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def run(self, limit=500):
        steps = 0
        while self.jobs and steps < limit:
            _, callback = self.jobs.popitem()
            callback()
            steps += 1
        return steps


class FakeWidget:
    def __init__(self):
        self.values = {}

    def configure(self, **values):
        self.values.update(values)

    def place_configure(self, **values):
        self.values.update(values)


def make_tray():
    tray = helper_overlay.HelperOverlay.__new__(helper_overlay.HelperOverlay)
    tray.theme = helper_overlay.DEFAULT_CONFIG["theme"]
    tray.blend_color = lambda first, _second, _amount: first
    tray.root = FakeRoot()
    tray._bottom_tray = FakeWidget()
    tray.quick_tools_handle = FakeWidget()
    tray.quick_tools_status = FakeWidget()
    tray._tray_anim_job = None
    tray._tray_hide_job = None
    tray._tray_open = False
    tray._tray_anim_progress = 0.0
    # Mirror the shipping geometry rather than pinning old numbers, so the
    # test keeps covering the animation when the tray is resized.
    tray._tray_target_h = 208
    tray._tray_peek_h = 24
    tray._tray_open_relwidth = 0.44
    tray._tray_peek_relwidth = 0.13
    tray._tray_current_h = tray._tray_peek_h
    tray._tray_current_relwidth = tray._tray_peek_relwidth
    return tray


def test_repeated_hover_uses_one_animation_loop():
    tray = make_tray()

    for _ in range(100):
        tray._tray_show()

    assert len(tray.root.jobs) == 1
    assert tray.root.run() < 50
    assert tray.root.jobs == {}
    assert tray._tray_current_h == pytest.approx(tray._tray_target_h)
    assert tray._tray_current_relwidth == pytest.approx(tray._tray_open_relwidth)


def test_close_and_mid_animation_reversal_finish_cleanly():
    tray = make_tray()
    tray._tray_show()
    active_job = tray._tray_anim_job

    tray._tray_close()

    assert tray._tray_anim_job == active_job
    assert len(tray.root.jobs) == 1
    assert tray.root.run() < 50
    assert tray.root.jobs == {}
    assert tray._tray_current_h == pytest.approx(tray._tray_peek_h)
    assert tray._tray_current_relwidth == pytest.approx(tray._tray_peek_relwidth)


def test_width_and_height_finish_together_so_opening_does_not_jump():
    tray = make_tray()
    tray._tray_show()

    ratios = []
    while tray.root.jobs:
        _, callback = tray.root.jobs.popitem()
        callback()
        height_span = tray._tray_target_h - tray._tray_peek_h
        width_span = tray._tray_open_relwidth - tray._tray_peek_relwidth
        ratios.append((
            (tray._tray_current_h - tray._tray_peek_h) / height_span,
            (tray._tray_current_relwidth - tray._tray_peek_relwidth) / width_span,
        ))

    assert ratios, "the tray never animated"
    # Both axes are driven by one eased value, so they stay in lockstep;
    # animating them separately is what made the panel look like it jumped.
    for height_ratio, width_ratio in ratios:
        assert height_ratio == pytest.approx(width_ratio, abs=1e-6)
    assert ratios[-1][0] == pytest.approx(1.0)
