import asyncio
from pathlib import Path


def test_operator_viewer_exposes_a_phone_keyboard_bridge():
    root = Path(__file__).resolve().parents[1]
    viewer = (root / "director/operator/viewer.html").read_text(encoding="utf-8")
    assert 'id="keyboardButton"' in viewer
    assert 'id="keyboardInput"' in viewer
    assert "director.open-keyboard" in viewer
    assert "director.keyboard-input" in viewer
    assert "director.keyboard-key" in viewer
    assert "handleKeyboardInput" in viewer
    assert "rfb.sendKey" in viewer
    assert "deleteContentBackward" in viewer


def test_healthy_viewer_connection_does_not_probe_or_start_operator(monkeypatch):
    from director.operator import display

    calls = {"connect": 0, "ensure": 0}
    connection = (object(), object())

    async def fake_connect(host, port):
        calls["connect"] += 1
        assert (host, port) == ("127.0.0.1", 6123)
        return connection

    async def fake_ensure(settings=None, *, with_chrome=False):
        calls["ensure"] += 1

    monkeypatch.setattr(asyncio, "open_connection", fake_connect)
    monkeypatch.setattr(display, "ensure_running", fake_ensure)

    got = asyncio.run(display.open_viewer_connection(
        {"operator": {"vnc_port": 6123}}))

    assert got is connection
    assert calls == {"connect": 1, "ensure": 0}


def test_viewer_recovery_never_starts_chrome(monkeypatch):
    from director.operator import display

    display.reset_ready_cache()
    calls = {"connect": 0, "chrome_values": []}
    connection = (object(), object())

    async def fake_connect(host, port):
        calls["connect"] += 1
        if calls["connect"] < 3:
            raise ConnectionRefusedError("not listening yet")
        return connection

    async def fake_ensure(settings=None, *, with_chrome=False):
        calls["chrome_values"].append(with_chrome)
        return {"ready": True}

    monkeypatch.setattr(asyncio, "open_connection", fake_connect)
    monkeypatch.setattr(display, "ensure_running", fake_ensure)

    got = asyncio.run(display.open_viewer_connection({"operator": {}}))

    assert got is connection
    assert calls == {"connect": 3, "chrome_values": [False]}


def test_restart_viewer_preserves_display_and_browser(monkeypatch):
    from director.operator import display

    commands = []

    async def fake_run(argv, timeout=20, env=None):
        commands.append(argv)
        return 0, ""

    async def fake_status(settings=None):
        return {"ready": True}

    monkeypatch.setattr(display, "_run", fake_run)
    monkeypatch.setattr(display, "status", fake_status)

    asyncio.run(display.restart_viewer())

    assert commands == [[
        "systemctl", "--user", "restart", display.UNITS["vnc"],
    ]]


def test_real_desktop_units_attach_to_xorg_and_retire_xvfb():
    root = Path(__file__).resolve().parents[1]
    vnc = (root / "director/deploy/aios-director-x11vnc.service").read_text()
    chrome = (root / "director/deploy/aios-director-chrome.service").read_text()
    install = (root / "director/deploy/install.sh").read_text()
    desktop = (root / "director/deploy/real-desktop-start.sh").read_text()

    assert "-display :0" in vnc
    assert "-auth /run/user/1000/gdm/Xauthority" in vnc
    assert "Environment=DISPLAY=:0" in chrome
    assert "disable --now aios-director-xvfb.service aios-director-wm.service" in install
    assert "--mode 1280x720 --scale 1x1" in desktop
    assert "DBUS_SESSION_BUS_ADDRESS" in desktop
    assert "org.gnome.desktop.session idle-delay 0" in desktop
    assert "org.gnome.desktop.screensaver lock-enabled false" in desktop
    assert "org.gnome.desktop.screensaver idle-activation-enabled false" in desktop
    assert "org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'" in desktop
    assert "org.gnome.ScreenSaver.SetActive false" in desktop
    assert "aios-director-real-desktop.service" in install


def test_real_desktop_environment_uses_gdm_xauthority():
    from director.operator import display

    env = display.display_env({"operator": {"mode": "real", "display": ":0",
                                            "xauthority": "/run/user/1000/gdm/Xauthority"}})
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/run/user/1000/gdm/Xauthority"
