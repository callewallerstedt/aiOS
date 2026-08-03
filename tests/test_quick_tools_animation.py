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
    tray._tray_current_h = 30
    tray._tray_current_relwidth = 0.20
    tray._tray_open = False
    tray._tray_target_h = 268
    tray._tray_peek_h = 30
    tray._tray_open_relwidth = 0.88
    tray._tray_peek_relwidth = 0.20
    return tray


def test_repeated_hover_uses_one_animation_loop():
    tray = make_tray()

    for _ in range(100):
        tray._tray_show()

    assert len(tray.root.jobs) == 1
    assert tray.root.run() < 50
    assert tray.root.jobs == {}
    assert (tray._tray_current_h, tray._tray_current_relwidth) == (268, 0.88)


def test_close_and_mid_animation_reversal_finish_cleanly():
    tray = make_tray()
    tray._tray_show()
    active_job = tray._tray_anim_job

    tray._tray_close()

    assert tray._tray_anim_job == active_job
    assert len(tray.root.jobs) == 1
    assert tray.root.run() < 50
    assert tray.root.jobs == {}
    assert (tray._tray_current_h, tray._tray_current_relwidth) == (30, 0.20)
