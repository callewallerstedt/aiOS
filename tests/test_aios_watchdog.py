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

    def test_load_config_handles_invalid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "helper_config.json"
            path.write_text("not json", encoding="utf-8")
            with mock.patch.object(aios_watchdog, "CONFIG_PATH", path):
                self.assertEqual(aios_watchdog.load_config(), {})


if __name__ == "__main__":
    unittest.main()
