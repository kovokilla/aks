"""Offline contract tests for the Cloud Shell backend initializer."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import init_backend


class InitBackendTests(unittest.TestCase):
    def setUp(self):
        self.config = init_backend.load_config(init_backend.DEFAULT_CONFIG)
        self.tools = mock.patch.object(init_backend.shutil, "which", side_effect=lambda name: "/tools/" + name)
        self.version = mock.patch.object(init_backend.subprocess, "check_output",
                                        return_value=json.dumps({"terraform_version": "1.13.3"}))
        self.azure = mock.patch.object(init_backend.Azure, "call", return_value={
            "id": self.config["subscription_id"],
            "tenantId": self.config["tenant_id"], "state": "Enabled",
        })
        self.run = mock.patch.object(init_backend.subprocess, "run",
                                    return_value=subprocess.CompletedProcess([], 0))
        self.tools_mock = self.tools.start()
        self.version_mock = self.version.start()
        self.azure_mock = self.azure.start()
        self.run_mock = self.run.start()
        self.addCleanup(mock.patch.stopall)

    def call_main(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return init_backend.main()

    def test_init_only_and_temporary_cli_backend(self):
        def run(command, **kwargs):
            self.assertIn("init", command)
            self.assertNotIn("apply", command)
            self.assertNotIn("destroy", command)
            self.assertIn("-lockfile=readonly", command)
            path = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("-backend-config=")))
            self.assertIn("use_oidc = false", path.read_text())
            self.assertIn("use_cli = true", path.read_text())
            self.backend_path = path
            self.assertEqual(kwargs["env"]["ARM_USE_CLI"], "true")
            return subprocess.CompletedProcess(command, 0)
        self.run_mock.side_effect = run
        self.assertEqual(self.call_main(), 0)
        self.assertFalse(self.backend_path.exists())

    def test_missing_tools_fail_before_azure(self):
        self.tools_mock.side_effect = lambda name: None
        self.assertEqual(self.call_main(), 1)
        self.azure_mock.assert_not_called()
        self.run_mock.assert_not_called()

    def test_wrong_version_fails_before_azure(self):
        self.version_mock.return_value = json.dumps({"terraform_version": "1.8.0"})
        self.assertEqual(self.call_main(), 1)
        self.azure_mock.assert_not_called()
        self.run_mock.assert_not_called()

    def test_wrong_tenant_fails_before_init(self):
        self.azure_mock.return_value["tenantId"] = "33333333-3333-4333-8333-333333333333"
        self.assertEqual(self.call_main(), 1)
        self.run_mock.assert_not_called()

    def test_wrong_subscription_fails_before_init(self):
        self.azure_mock.return_value["id"] = "33333333-3333-4333-8333-333333333333"
        self.assertEqual(self.call_main(), 1)
        self.run_mock.assert_not_called()

    def test_inherited_auth_and_cli_injection_are_removed(self):
        with mock.patch.dict(init_backend.os.environ, {
            "ARM_CLIENT_SECRET": "synthetic-test-only",
            "ARM_USE_OIDC": "true",
            "TF_CLI_ARGS_init": "-backend-config=unexpected",
        }):
            self.assertEqual(self.call_main(), 0)
        env = self.run_mock.call_args.kwargs["env"]
        self.assertNotIn("ARM_CLIENT_SECRET", env)
        self.assertNotIn("TF_CLI_ARGS_init", env)
        self.assertEqual(env["ARM_USE_OIDC"], "false")

    def test_init_failure_propagates(self):
        self.run_mock.return_value = subprocess.CompletedProcess([], 3)
        self.assertEqual(self.call_main(), 3)


if __name__ == "__main__":
    unittest.main()
