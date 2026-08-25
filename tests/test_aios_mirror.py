from __future__ import annotations

from types import SimpleNamespace
import urllib.parse
import urllib.request

from aios_ui import mirror
from aios_ui.server import UIServer


def test_mirror_bootstrap_redirects_to_real_token_gated_ui():
    server = UIServer(("127.0.0.1", 0), mirror_bootstrap=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/mirror", timeout=3) as response:
            final = urllib.parse.urlparse(response.geturl())
            query = urllib.parse.parse_qs(final.query)
            assert final.path == "/index.html"
            assert query["phone"] == ["1"]
            assert query["token"] == [server.token]
            assert b'<div id="shell" class="shell">' in response.read()

        with urllib.request.urlopen(f"{base}/mirror-health", timeout=3) as response:
            assert response.status == 200
            assert b'aios-mirror' in response.read()
    finally:
        server.stop()


def test_connected_devices_keeps_only_authorised_adb_devices(monkeypatch):
    output = "List of devices attached\nphone-1\tdevice\nphone-2\tunauthorized\nphone-3\toffline\n"
    monkeypatch.setattr(
        mirror,
        "_adb",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )

    assert mirror.connected_devices() == {"phone-1"}
