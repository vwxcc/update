import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_deploy_daemon as updater


class CurrentReleaseLayoutTests(unittest.TestCase):
    def setUp(self):
        updater.LOG = logging.getLogger("test_ai_ch_updater")

    def make_release(self, root: Path) -> None:
        (root / "app").mkdir(parents=True)
        (root / "frontend").mkdir(parents=True)
        tests = root / "tests"
        tests.mkdir(parents=True)
        (root / "app" / "main.py").write_text("app = object()\n", encoding="utf-8")
        (root / "frontend" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        (root / "requirements.txt").write_text("fastapi==0.115.6\n", encoding="utf-8")
        for name in (
            "test_scheduled.py",
            "test_auth_contract.py",
            "test_chats_search.py",
            "test_frontend_build.py",
            "test_database_migrations.py",
        ):
            (tests / name).write_text("import unittest\n", encoding="utf-8")

    def test_release_without_sandbox_is_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_release(root)
            updater.validate_release(root)

    def test_release_requires_current_app_entrypoints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app").mkdir()
            (root / "frontend").mkdir()
            (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "app/main.py"):
                updater.validate_release(root)

    def test_old_failed_sha_is_retried_after_updater_upgrade(self):
        sha = "a" * 40
        old_state = {"failed_sha": sha}
        self.assertFalse(updater.failed_commit_for_current_updater(old_state, sha))
        old_state["failed_updater_version"] = updater.UPDATER_VERSION
        self.assertTrue(updater.failed_commit_for_current_updater(old_state, sha))

    def test_health_check_runs_inside_selected_container(self):
        completed = subprocess.CompletedProcess(["docker"], 0, "", "")
        with (
            patch.object(updater, "docker_exec", return_value=completed) as docker_exec,
            patch.object(updater, "HEALTH_TIMEOUT", 15),
            patch.object(updater, "DOCKER_CONTAINER", "ai-ch-mega"),
            patch.object(updater, "CONTAINER_PORT", 8000),
        ):
            self.assertTrue(updater.wait_for_health())
        docker_exec.assert_called_once()
        script = docker_exec.call_args.args[2]
        self.assertIn("127.0.0.1:8000/api/health", script)

    def test_removed_source_files_are_pruned_after_success(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            previous = base / "previous"
            release = base / "release"
            for root in (previous, release):
                (root / "app").mkdir(parents=True)
                (root / "frontend").mkdir(parents=True)
            (previous / "app" / "obsolete.py").write_text("old = True\n", encoding="utf-8")
            (release / "app" / "main.py").write_text("new = True\n", encoding="utf-8")
            (previous / "frontend" / "old.js").write_text("old()", encoding="utf-8")
            (release / "frontend" / "index.html").write_text("new", encoding="utf-8")
            completed = subprocess.CompletedProcess(["docker"], 0, "", "")
            with patch.object(updater, "docker_exec", return_value=completed) as docker_exec:
                updater.prune_removed_files(previous, release)
            args = docker_exec.call_args.args
            self.assertIn("/app/app/obsolete.py", args)
            self.assertIn("/app/frontend/old.js", args)


if __name__ == "__main__":
    unittest.main()
