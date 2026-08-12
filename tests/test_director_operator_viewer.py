import asyncio
from pathlib import Path


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


def test_xvfb_is_owned_and_restarted_by_systemd():
    root = Path(__file__).resolve().parents[1]
    unit = (root / "director/deploy/aios-director-xvfb.service").read_text()
    launcher = (root / "director/deploy/xvfb-start.sh").read_text()

    assert "KillMode=control-group" in unit
    assert "Restart=always" in unit
    assert "exec tail --pid=" not in launcher
    assert "exec /usr/bin/Xvfb" in launcher
