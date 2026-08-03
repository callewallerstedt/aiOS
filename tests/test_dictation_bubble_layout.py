import voice_dictation


class _Font:
    def measure(self, text):
        return len(str(text)) * 8

    def metrics(self, name):
        assert name == "linespace"
        return 16


class _Root:
    @staticmethod
    def winfo_screenheight():
        return 1080


def _overlay():
    overlay = voice_dictation.MicOverlay.__new__(voice_dictation.MicOverlay)
    font = _Font()
    overlay.root = _Root()
    overlay.compose_width = 470
    overlay.compose_target = "agent"
    overlay.compose_history = []
    overlay.compose_text = ""
    overlay.compose_note = ""
    overlay.compose_tools = []
    overlay.compose_reply = ""
    overlay.compose_error = ""
    overlay._compose_cache = (None, [], 0)
    overlay._pill_anchor = None
    overlay.pill_size = (236, 52)
    overlay.compose_gap = 10
    overlay.panel_bg = "panel"
    overlay.text = "text"
    overlay.muted = "muted"
    overlay.accent = "accent"
    overlay.danger = "danger"
    overlay.font_body = font
    overlay.font_compose = font
    overlay.font_reply = font
    overlay.font_tool = font
    overlay.blend_color = lambda _background, foreground, amount: f"{foreground}:{amount}"
    return overlay


def _bubble_ops(overlay):
    return [op for op in overlay._compose_build()[0] if op[0] == "bubble"]


def test_long_unbroken_user_text_wraps_without_leaving_its_bubble():
    overlay = _overlay()
    transcript = "U" * 240
    overlay.compose_text = transcript

    user_bubble = _bubble_ops(overlay)[0]
    _, _x, _y, width, _height, _bg, lines, _fill, font, _line_height = user_bubble

    assert len(lines) > 1
    assert "".join(lines) == transcript
    assert all(
        font.measure(line) <= width - voice_dictation.BUBBLE_PAD_X * 2
        for line in lines
    )


def test_user_and_agent_text_stay_padded_and_visually_separate():
    overlay = _overlay()
    overlay.compose_history = [
        ("assistant", "Agent reply wraps neatly inside the neutral bubble."),
        ("user", "User message wraps neatly inside the accent bubble."),
    ]

    agent_bubble, user_bubble = _bubble_ops(overlay)
    for bubble in (agent_bubble, user_bubble):
        _, _x, _y, width, _height, _bg, lines, _fill, font, _line_height = bubble
        assert all(
            font.measure(line) <= width - voice_dictation.BUBBLE_PAD_X * 2
            for line in lines
        )

    assert agent_bubble[1] < user_bubble[1]
    assert agent_bubble[5] != user_bubble[5]
