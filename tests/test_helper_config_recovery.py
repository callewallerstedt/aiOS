import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import helper_overlay


class HelperConfigRecoveryTests(unittest.TestCase):
    def test_save_keeps_previous_valid_config_as_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "helper_config.json"
            backup = Path(folder) / "helper_config.json.bak"
            config.write_text(json.dumps({"theme": {"accent": "#112233"}}), encoding="utf-8")

            with mock.patch.object(helper_overlay, "CONFIG_PATH", config), \
                    mock.patch.object(helper_overlay, "CONFIG_BACKUP_PATH", backup):
                helper_overlay.save_config({"theme": {"accent": "#445566"}})

            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["theme"]["accent"], "#445566")
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["theme"]["accent"], "#112233")

    def test_load_recovers_invalid_primary_from_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "helper_config.json"
            backup = Path(folder) / "helper_config.json.bak"
            config.write_text("", encoding="utf-8")
            backup.write_text(json.dumps({"theme": {"accent": "#aabbcc"}}), encoding="utf-8")

            with mock.patch.object(helper_overlay, "CONFIG_PATH", config), \
                    mock.patch.object(helper_overlay, "CONFIG_BACKUP_PATH", backup):
                loaded = helper_overlay.load_config()

            self.assertEqual(loaded["theme"]["accent"], "#aabbcc")
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["theme"]["accent"], "#aabbcc")

    def test_stale_loaded_config_preserves_concurrent_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "helper_config.json"
            backup = Path(folder) / "helper_config.json.bak"
            config.write_text(json.dumps({
                "theme": {"always_on_top": True, "accent": "#111111"},
                "openrouter_enabled_models": ["old/model"],
            }), encoding="utf-8")

            with mock.patch.object(helper_overlay, "CONFIG_PATH", config), \
                    mock.patch.object(helper_overlay, "CONFIG_BACKUP_PATH", backup):
                stale = helper_overlay.load_config()
                concurrent = json.loads(config.read_text(encoding="utf-8"))
                concurrent["theme"]["always_on_top"] = False
                concurrent["openrouter_enabled_models"] = ["old/model", "stealth/ox-alpha"]
                concurrent["model_configs"] = [{"id": "ox-alpha", "name": "Ox Alpha"}]
                config.write_text(json.dumps(concurrent), encoding="utf-8")

                stale["theme"]["accent"] = "#222222"
                helper_overlay.save_config(stale)

            saved = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(saved["theme"]["accent"], "#222222")
            self.assertFalse(saved["theme"]["always_on_top"])
            self.assertIn("stealth/ox-alpha", saved["openrouter_enabled_models"])
            self.assertEqual(saved["model_configs"][0]["id"], "ox-alpha")


class PhonePairingPreservationTests(unittest.TestCase):
    def save(self, folder, stored, incoming):
        config = Path(folder) / "helper_config.json"
        backup = Path(folder) / "helper_config.json.bak"
        config.write_text(json.dumps(stored), encoding="utf-8")
        with mock.patch.object(helper_overlay, "CONFIG_PATH", config), \
                mock.patch.object(helper_overlay, "CONFIG_BACKUP_PATH", backup):
            helper_overlay.save_config(incoming)
        return json.loads(config.read_text(encoding="utf-8"))

    def test_stale_save_cannot_blank_the_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = self.save(
                folder,
                {"phone_relay": {"url": "https://relay", "machine_id": "m1",
                                 "machine_token": "secret", "enabled": True}},
                {"theme": {"accent": "#445566"},
                 "phone_relay": {"url": "https://relay", "machine_id": "",
                                 "machine_token": "", "enabled": False}},
            )
            self.assertEqual(saved["phone_relay"]["machine_id"], "m1")
            self.assertEqual(saved["phone_relay"]["machine_token"], "secret")
            self.assertTrue(saved["phone_relay"]["enabled"])
            self.assertEqual(saved["theme"]["accent"], "#445566")

    def test_a_new_pairing_still_replaces_the_old_one(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = self.save(
                folder,
                {"phone_relay": {"machine_id": "m1", "machine_token": "old", "enabled": True}},
                {"phone_relay": {"machine_id": "m2", "machine_token": "new", "enabled": True}},
            )
            self.assertEqual(saved["phone_relay"]["machine_id"], "m2")
            self.assertEqual(saved["phone_relay"]["machine_token"], "new")

    def test_a_config_without_a_relay_section_is_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = self.save(folder, {"theme": {}}, {"theme": {"accent": "#010203"}})
            self.assertNotIn("phone_relay", saved)


if __name__ == "__main__":
    unittest.main()
