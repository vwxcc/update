import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_deploy_daemon as updater


class SandboxProvisioningTests(unittest.TestCase):
    def setUp(self):
        updater.LOG = logging.getLogger("test_ai_ch_sandbox_provisioning")

    def test_probe_uses_secret_file_and_never_embeds_secret_value(self):
        script = updater.sandbox_probe_script(
            "http://ai-ch-sandbox:8000/ready",
            "/run/secrets/sandbox_api_key",
        )
        self.assertIn("Authorization", script)
        self.assertIn("/run/secrets/sandbox_api_key", script)
        self.assertNotIn("a-real-secret-value", script)

    def test_mount_source_uses_quoted_docker_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            completed = subprocess.CompletedProcess(
                ["docker", "inspect"], 0, temp, ""
            )
            with patch.object(updater, "run_cmd", return_value=completed) as run_cmd:
                result = updater.sandbox_app_data_source()
            self.assertEqual(result, Path(temp).resolve())
            command = run_cmd.call_args.args[0]
            self.assertIn('eq .Destination "/app/data"', command[3])

    def test_sandbox_secret_script_is_valid_python_and_appends_newline(self):
        script = updater.sandbox_secret_script()
        compile(script, "<sandbox-secret-script>", "exec")
        self.assertIn("tmp.write_text(key+chr(10),encoding='utf-8')", script)

    def test_internal_network_is_created_and_connected_to_app(self):
        def fake_run(args, **kwargs):
            if args[:3] == ["docker", "network", "inspect"]:
                return subprocess.CompletedProcess(args, 1, "", "not found")
            if args[:3] == ["docker", "inspect", "--format={{json .NetworkSettings.Networks}}"]:
                return subprocess.CompletedProcess(args, 0, '{"bridge":{}}', "")
            return subprocess.CompletedProcess(args, 0, "ok", "")

        with patch.object(updater, "run_cmd", side_effect=fake_run) as run_cmd:
            updater.ensure_sandbox_network()
        commands = [call.args[0] for call in run_cmd.call_args_list]
        self.assertIn(
            ["docker", "network", "create", "--internal", updater.SANDBOX_NETWORK],
            commands,
        )
        self.assertIn(
            ["docker", "network", "connect", updater.SANDBOX_NETWORK, updater.DOCKER_CONTAINER],
            commands,
        )

    def test_runner_flags_are_isolated_and_have_no_published_port(self):
        source = Path(updater.__file__).read_text(encoding="utf-8")
        self.assertIn('"--network", SANDBOX_NETWORK', source)
        self.assertIn('"--cap-drop", "ALL"', source)
        self.assertIn('"--cap-add", "CHOWN"', source)
        self.assertIn('"--cap-add", "SETUID"', source)
        self.assertIn('"--cap-add", "SETGID"', source)
        # The privileged runner supervisor needs these to collect/reset child-owned files.
        self.assertIn('"--cap-add", "DAC_OVERRIDE"', source)
        self.assertIn('"--cap-add", "FOWNER"', source)
        self.assertIn('"--read-only"', source)
        self.assertIn('"--pids-limit", "256"', source)
        self.assertIn('dst=/run/secrets/sandbox_api_key,readonly', source)
        self.assertIn('"--env", "SANDBOX_API_KEY_FILE=/run/secrets/sandbox_api_key"', source)
        self.assertNotIn('"--publish"', source)
        self.assertNotIn('"--publish-all"', source)
        # The builder uses a shell entrypoint; the committed runner and candidate
        # must explicitly launch Python, never pass "python" as a shell script path.
        self.assertIn("'ENTRYPOINT [\"python\"]'", source)
        self.assertIn("'CMD [\"/srv/server.py\"]'", source)
        self.assertIn('"--entrypoint", "python", image, "/srv/server.py"', source)

    def test_candidate_diagnostics_capture_state_and_startup_logs(self):
        def fake_run(args, **kwargs):
            if args[1] == "inspect":
                return subprocess.CompletedProcess(
                    args, 0, "restarting|exit=1|oom=false|error=|restarts=4", ""
                )
            if args[1] == "logs":
                return subprocess.CompletedProcess(
                    args, 0, "Traceback (most recent call last):\nRuntimeError: startup failed", ""
                )
            raise AssertionError(f"Unexpected command: {args}")

        with patch.object(updater, "run_cmd", side_effect=fake_run) as run_cmd:
            diagnostics = updater.sandbox_container_diagnostics("ai-ch-sandbox-candidate-test")

        self.assertIn("restarting|exit=1|oom=false", diagnostics)
        self.assertIn("RuntimeError: startup failed", diagnostics)
        self.assertEqual(run_cmd.call_count, 2)



if __name__ == "__main__":
    unittest.main()
