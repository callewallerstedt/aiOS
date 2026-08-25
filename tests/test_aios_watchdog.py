import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import aios_watchdog


class WatchdogTests(unittest.TestCase):
    def test_is_fresh_uses_heartbeat_age(self):
        with tempfile.TemporaryDirectory() as folder:
            heartbeat = Path(folder) / "heartbeat"
            heartbeat.touch()
            now = time.time()
            self.assertTrue(aios_watchdog.is_fresh(heartbeat, 10, now))
            self.assertFalse(aios_watchdog.is_fresh(heartbeat, 10, now + 11))

    def test_phone_enabled_requires_pairing_token(self):
        self.assertFalse(aios_watchdog.phone_enabled({}))
        self.assertFalse(aios_watchdog.phone_enabled({"phone_relay": {"enabled": True}}))
        self.assertTrue(aios_watchdog.phone_enabled({
            "phone_relay": {"enabled": True, "machine_token": "private"}
        }))

    def test_unpaired_desktop_starts_local_bridge(self):
        with mock.patch.object(aios_watchdog, "local_bridge_healthy", return_value=False), \
                mock.patch.object(aios_watchdog, "start_phone_bridge") as start:
            status, grace, started = aios_watchdog.ensure_phone_bridge({}, 100.0, 0.0)

        self.assertEqual(status, "local bridge restarting")
        self.assertEqual(grace, 145.0)
        self.assertTrue(started)
        start.assert_called_once_with()

    def test_unpaired_desktop_keeps_healthy_local_bridge(self):
        with mock.patch.object(aios_watchdog, "local_bridge_healthy", return_value=True), \
                mock.patch.object(aios_watchdog, "start_phone_bridge") as start:
            status, grace, started = aios_watchdog.ensure_phone_bridge({}, 100.0, 0.0)

        self.assertEqual(status, "not paired")
        self.assertEqual(grace, 0.0)
        self.assertFalse(started)
        start.assert_not_called()

    def test_load_config_handles_invalid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "helper_config.json"
            path.write_text("not json", encoding="utf-8")
            with mock.patch.object(aios_watchdog, "CONFIG_PATH", path):
                self.assertEqual(aios_watchdog.load_config(), {})

    def test_bridge_start_passes_an_explicit_python_runtime(self):
        with mock.patch.object(aios_watchdog, "find_pythonw", return_value=r"C:\Python\pythonw.exe"), \
                mock.patch.object(aios_watchdog, "spawn") as spawn:
            aios_watchdog.start_phone_bridge()

        command = spawn.call_args.args[0]
        self.assertEqual(command[-2:], ["-PythonExe", r"C:\Python\pythonw.exe"])
        self.assertEqual(spawn.call_args.kwargs["output_path"], aios_watchdog.BRIDGE_START_LOG)

    def test_stale_director_client_is_restarted(self):
        with mock.patch.object(aios_watchdog, "director_enabled", return_value=True), \
                mock.patch.object(aios_watchdog, "is_fresh", return_value=False), \
                mock.patch.object(aios_watchdog, "stop_python_script") as stop, \
                mock.patch.object(aios_watchdog, "spawn") as spawn:
            status, grace, started = aios_watchdog.ensure_director_client(
                r"C:\Python\pythonw.exe", 100.0, 0.0)

        self.assertEqual((status, grace, started), ("restarting", 145.0, True))
        stop.assert_called_once_with("director_client.py")
        spawn.assert_called_once_with([
            r"C:\Python\pythonw.exe", str(aios_watchdog.BASE_DIR / "director_client.py")])

    def test_fast_start_launches_the_whole_desktop_stack_without_waiting(self):
        commands = []
        with mock.patch.object(aios_watchdog.time, "time", return_value=100.0), \
                mock.patch.object(aios_watchdog, "find_autohotkey", return_value=r"C:\AutoHotkey.exe"), \
                mock.patch.object(aios_watchdog, "spawn", side_effect=lambda command, **_kwargs: commands.append(command)), \
                mock.patch.object(aios_watchdog, "log"), \
                mock.patch.object(aios_watchdog, "start_phone_bridge") as bridge:
            grace = aios_watchdog.fast_start_stack(r"C:\Python\pythonw.exe")

        self.assertEqual(grace, (155.0, 145.0, 135.0, 145.0))
        self.assertIn(
            [r"C:\Python\pythonw.exe", str(aios_watchdog.BASE_DIR / "aios_shell.py")],
            commands,
        )
        self.assertIn(
            [r"C:\Python\pythonw.exe", str(aios_watchdog.BASE_DIR / "helper_overlay.py"), "--background", "--tk"],
            commands,
        )
        self.assertIn([r"C:\AutoHotkey.exe", str(aios_watchdog.BASE_DIR / "autocorrect.ahk")], commands)
        bridge.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
