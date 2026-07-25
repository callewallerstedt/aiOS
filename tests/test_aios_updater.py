import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aios_updater


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


class GitUpdateTests(unittest.TestCase):
    """The git path has to actually move the working tree onto the upstream
    tip — that's the whole point of pressing Update."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.upstream = root / "upstream"
        self.upstream.mkdir()
        git("init", "-q", "-b", "main", cwd=self.upstream)
        (self.upstream / "helper_overlay.py").write_text("v1\n", encoding="utf-8")
        git("add", "-A", cwd=self.upstream)
        git("commit", "-qm", "v1", cwd=self.upstream)

        self.clone = root / "clone"
        git("clone", "-q", str(self.upstream), str(self.clone), cwd=root)

        self.src = {"owner": "o", "repo": "r", "branch": "main", "token": ""}
        patches = [
            mock.patch.object(aios_updater, "BASE_DIR", self.clone),
            mock.patch.object(aios_updater, "HELPER_CONFIG_PATH",
                              self.clone / "helper_config.json"),
            mock.patch.object(aios_updater, "_git_remote_url",
                              lambda src, with_token=False: str(self.upstream)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.progress = []

    def _emit(self, msg):
        self.progress.append(msg)

    def _push_upstream(self, name="helper_overlay.py", body="v2\n"):
        (self.upstream / name).write_text(body, encoding="utf-8")
        git("add", "-A", cwd=self.upstream)
        git("commit", "-qm", "v2", cwd=self.upstream)
        return git("rev-parse", "HEAD", cwd=self.upstream)

    def test_pulls_latest_commit(self):
        head = self._push_upstream()
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), head)
        self.assertEqual((self.clone / "helper_overlay.py").read_text(), "v2\n")

    def test_perform_update_end_to_end_on_a_real_clone(self):
        """What the Update button actually calls."""
        head = self._push_upstream()
        with mock.patch.object(aios_updater, "_install_deps",
                               return_value=(True, "deps ok")) as deps:
            result = aios_updater.perform_update(progress=self._emit)
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(result["via"], "git")
        self.assertTrue(result["restart_needed"])
        deps.assert_called_once()
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), head)
        self.assertEqual(result["current"], head[:7])

    def test_stays_on_a_branch_after_updating(self):
        self._push_upstream()
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.clone),
                         "main")

    def test_reattaches_a_detached_head(self):
        git("checkout", "-q", "--detach", cwd=self.clone)
        head = self._push_upstream()
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.clone),
                         "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), head)

    def test_local_edits_are_discarded(self):
        head = self._push_upstream()
        (self.clone / "helper_overlay.py").write_text("hand edited\n", encoding="utf-8")
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertEqual((self.clone / "helper_overlay.py").read_text(), "v2\n")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), head)

    def test_untracked_collision_does_not_block_the_update(self):
        """A file the user already has locally that upstream then adds used to
        make `checkout` bail out and the whole update fail."""
        (self.clone / "new_module.py").write_text("stale local copy\n", encoding="utf-8")
        self._push_upstream(name="new_module.py", body="upstream copy\n")
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertEqual((self.clone / "new_module.py").read_text(), "upstream copy\n")

    def test_untracked_user_data_survives(self):
        cfg = self.clone / "helper_config.json"
        cfg.write_text('{"secret": 1}', encoding="utf-8")
        self._push_upstream()
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertEqual(cfg.read_text(), '{"secret": 1}')

    def test_follows_the_configured_source_not_the_stale_origin(self):
        """origin points at a repo that no longer exists; the configured
        owner/repo must still win."""
        git("remote", "set-url", "origin", "https://github.com/gone/gone.git",
            cwd=self.clone)
        head = self._push_upstream()
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), head)

    def test_stale_tarball_marker_is_cleared_after_a_git_update(self):
        """A marker left behind by a tarball fallback would otherwise keep
        reporting the old version forever."""
        (self.clone / ".aios_sha").write_text("0000000", encoding="utf-8")
        head = self._push_upstream()
        ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertTrue(ok, msg)
        self.assertFalse((self.clone / ".aios_sha").exists())
        self.assertEqual(aios_updater.get_current_sha(), head[:7])

    def test_tarball_marker_wins_over_head_in_a_git_clone(self):
        """The fallback path overwrites files without moving HEAD, so the
        marker is what actually describes the install."""
        (self.clone / ".aios_sha").write_text("abcdef1234", encoding="utf-8")
        self.assertEqual(aios_updater.get_current_sha(), "abcdef1234")

    def test_missing_git_reports_instead_of_hanging(self):
        with mock.patch.object(aios_updater, "_git_available", lambda: False):
            ok, msg = aios_updater._git_update(self.src, self._emit)
        self.assertFalse(ok)
        self.assertIn("git", msg)

    def test_fetch_failure_message_hides_the_token(self):
        src = dict(self.src, token="ghp_supersecret")
        with mock.patch.object(aios_updater, "_git_remote_url",
                               lambda s, with_token=False: "/nope/ghp_supersecret"), \
             mock.patch.object(aios_updater.time, "sleep", lambda _s: None):
            ok, msg = aios_updater._git_update(src, self._emit)
        self.assertFalse(ok)
        self.assertNotIn("ghp_supersecret", msg)


class UpdateNowTests(unittest.TestCase):
    def test_skips_work_when_already_up_to_date(self):
        check = {"ok": True, "behind": False, "current": "abc1234"}
        with mock.patch.object(aios_updater, "check_for_update", return_value=check), \
             mock.patch.object(aios_updater, "perform_update") as perform:
            result = aios_updater.update_now()
        perform.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["updated"])
        self.assertFalse(result["restart_needed"])

    def test_installs_when_behind(self):
        check = {"ok": True, "behind": True, "current": "abc1234", "latest": "def5678"}
        done = {"ok": True, "restart_needed": True, "message": "updated"}
        with mock.patch.object(aios_updater, "check_for_update", return_value=check), \
             mock.patch.object(aios_updater, "perform_update", return_value=done):
            result = aios_updater.update_now()
        self.assertTrue(result["updated"])
        self.assertTrue(result["restart_needed"])

    def test_force_installs_even_when_current(self):
        check = {"ok": True, "behind": False, "current": "abc1234", "latest": "abc1234"}
        done = {"ok": True, "restart_needed": True, "message": "updated"}
        with mock.patch.object(aios_updater, "check_for_update", return_value=check), \
             mock.patch.object(aios_updater, "perform_update", return_value=done) as perform:
            result = aios_updater.update_now(force=True)
        perform.assert_called_once()
        self.assertTrue(result["updated"])

    def test_reports_a_failed_check(self):
        check = {"ok": False, "error": "HTTP 404 from GitHub — repo or branch not found"}
        with mock.patch.object(aios_updater, "check_for_update", return_value=check), \
             mock.patch.object(aios_updater, "perform_update") as perform:
            result = aios_updater.update_now()
        perform.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("404", result["message"])


class PerformUpdateFallbackTests(unittest.TestCase):
    def test_git_failure_falls_back_to_the_tarball(self):
        with mock.patch.object(aios_updater, "_is_git_repo", return_value=True), \
             mock.patch.object(aios_updater, "_git_update",
                               return_value=(False, "git is not installed or not on PATH")), \
             mock.patch.object(aios_updater, "_tarball_update",
                               return_value=(True, "tarball staged")) as tarball:
            result = aios_updater.perform_update()
        tarball.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertTrue(result["staged"])
        self.assertTrue(result["restart_needed"])

    def test_both_paths_failing_surfaces_both_reasons(self):
        with mock.patch.object(aios_updater, "_is_git_repo", return_value=True), \
             mock.patch.object(aios_updater, "_git_update",
                               return_value=(False, "git fetch failed: offline")), \
             mock.patch.object(aios_updater, "_tarball_update",
                               return_value=(False, "tarball download failed")):
            result = aios_updater.perform_update()
        self.assertFalse(result["ok"])
        self.assertIn("git fetch failed", result["message"])
        self.assertIn("tarball download failed", result["message"])


class MiscTests(unittest.TestCase):
    def test_get_current_branch_falls_back_to_configured_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with mock.patch.object(aios_updater, "BASE_DIR", base), \
                 mock.patch.object(aios_updater, "HELPER_CONFIG_PATH",
                                   base / "helper_config.json"):
                self.assertEqual(aios_updater.get_current_branch(),
                                 aios_updater.DEFAULT_BRANCH)

    def test_empty_staging_dir_is_not_a_pending_update(self):
        with tempfile.TemporaryDirectory() as folder:
            staging = Path(folder) / ".aios_update_staging"
            staging.mkdir()
            with mock.patch.object(aios_updater, "STAGING_DIR", staging):
                self.assertFalse(aios_updater._has_staged_files())
                (staging / "helper_overlay.py").write_text("x", encoding="utf-8")
                self.assertTrue(aios_updater._has_staged_files())

    def test_tarball_urls_prefer_the_api_when_a_token_is_set(self):
        src = {"owner": "o", "repo": "r", "branch": "main", "token": "ghp_x"}
        self.assertIn("api.github.com", aios_updater._tarball_urls(src)[0])
        public = aios_updater._tarball_urls(dict(src, token=""))
        self.assertIn("codeload.github.com", public[0])
        self.assertIn("api.github.com", public[1])

    def test_remote_url_only_embeds_the_token_when_asked(self):
        src = {"owner": "o", "repo": "r", "branch": "main", "token": "ghp_x"}
        self.assertNotIn("ghp_x", aios_updater._git_remote_url(src))
        self.assertIn("ghp_x", aios_updater._git_remote_url(src, with_token=True))


if __name__ == "__main__":
    unittest.main()
