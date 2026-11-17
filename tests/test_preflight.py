import logging
import subprocess
import unittest
from unittest.mock import patch

import auto_deploy_daemon as updater


class StagedStartupSmokeTests(unittest.TestCase):
    def setUp(self):
        updater.LOG = logging.getLogger("test_ai_ch_preflight")

    def test_smoke_test_uses_isolated_storage_and_clean_environment(self):
        result = subprocess.CompletedProcess(["docker"], 0, "startup ok", "")
        with (
            patch.object(updater, "run_cmd", return_value=result) as run_cmd,
            patch.object(updater, "docker_exec", return_value=result) as docker_exec,
        ):
            updater.smoke_test_release("/tmp/release-stage", "a" * 40)

        command = run_cmd.call_args.args[0]
        self.assertEqual(command[:4], ["docker", "exec", "-w", "/tmp/release-stage"])
        self.assertIn("env", command)
        self.assertIn("-i", command)
        self.assertIn("DATA_DIR=/tmp/release-stage/.updater-smoke-data", command)
        self.assertIn("UPLOAD_DIR=/tmp/release-stage/.updater-smoke-uploads", command)
        self.assertIn("FRONTEND_DIR=/tmp/release-stage/frontend", command)
        self.assertIn("SANDBOX_URL=", command)
        self.assertIn("SANDBOX_API_KEY=", command)
        self.assertIn(
            "SANDBOX_API_KEY_FILE=/tmp/release-stage/.updater-smoke-sandbox-key",
            command,
        )
        self.assertNotIn("DATA_DIR=/app/data", command)
        self.assertNotIn("UPLOAD_DIR=/app/uploads", command)
        self.assertIn("/tmp/release-stage", command[-1])
        self.assertIn("/opt/ai-ch-deps/" + "a" * 40, command[-1])
        self.assertEqual(docker_exec.call_count, 2)
        docker_exec.assert_any_call(
            "mkdir", "-p",
            "/tmp/release-stage/.updater-smoke-data",
            "/tmp/release-stage/.updater-smoke-uploads",
            timeout=15,
        )
        docker_exec.assert_any_call(
            "touch",
            "/tmp/release-stage/.updater-smoke-sandbox-key",
            timeout=10,
        )
        self.assertIn("app.router.lifespan_context(app)", command[-1])

    def test_smoke_test_without_dependency_change_uses_existing_site_path(self):
        result = subprocess.CompletedProcess(["docker"], 0, "startup ok", "")
        with patch.object(updater, "run_cmd", return_value=result) as run_cmd:
            updater.smoke_test_release("/tmp/release-stage", None)
        script = run_cmd.call_args.args[0][-1]
        self.assertIn("if ''", script)
        self.assertNotIn("/opt/ai-ch-deps/", script)

    def test_failed_startup_stops_update_before_container_switch(self):
        result = subprocess.CompletedProcess(["docker"], 1, "", "NameError: Session is not defined")
        with patch.object(updater, "run_cmd", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "предварительную проверку"):
                updater.smoke_test_release("/tmp/release-stage", None)


if __name__ == "__main__":
    unittest.main()
