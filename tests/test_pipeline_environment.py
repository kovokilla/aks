"""Private-project/context checks are mocked; native deployment gates stay in YAML."""

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_pipeline_environment as checks


def environment():
    return {
        "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example/",
        "SYSTEM_TEAMPROJECTID": "11111111-1111-4111-8111-111111111111",
        "ENVIRONMENT_ID": "42", "ENVIRONMENT_NAME": "aks-sandbox",
        "SYSTEM_ACCESSTOKEN": "synthetic-oauth-not-a-real-token",
    }


def project(visibility="private"):
    return {"id": environment()["SYSTEM_TEAMPROJECTID"], "visibility": visibility}


class EnvironmentContextTests(unittest.TestCase):
    def test_official_project_endpoint(self):
        url = checks.project_url(environment())
        self.assertEqual(url, "https://dev.azure.com/example/_apis/projects/"
                         + environment()["SYSTEM_TEAMPROJECTID"] + "?api-version=7.1")

    def test_untrusted_url_or_bad_environment_id_rejected(self):
        for key, value in (
            ("SYSTEM_COLLECTIONURI", "https://attacker.invalid/"),
            ("SYSTEM_COLLECTIONURI", "http://dev.azure.com/example/"),
            ("ENVIRONMENT_ID", "$(Environment.Id)"), ("ENVIRONMENT_ID", "0"),
            ("SYSTEM_TEAMPROJECTID", "not-a-project-id"),
        ):
            with self.subTest(key=key, value=value):
                env = environment()
                env[key] = value
                with self.assertRaises(ValueError):
                    checks.project_url(env)

    @mock.patch.object(checks.urllib.request, "build_opener")
    def test_preflight_reads_only_project_metadata_not_check_configuration(self, build_opener):
        build_opener.return_value.open.return_value = io.BytesIO(json.dumps(project()).encode())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            checks.verify(environment())
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("/_apis/projects/", request.full_url)
        self.assertNotIn("/checks/", request.full_url)
        self.assertEqual(build_opener.return_value.open.call_count, 1)
        self.assertIn("does not audit check presence", output.getvalue())
        self.assertNotIn("Verified enabled Approval", output.getvalue())

    @mock.patch.object(checks.urllib.request, "build_opener")
    def test_public_or_missing_visibility_rejected(self, build_opener):
        for value in ("public", None):
            build_opener.return_value.open.return_value = io.BytesIO(json.dumps(project(value)).encode())
            with self.assertRaisesRegex(ValueError, "private Azure DevOps project"):
                checks.verify(environment())

    @mock.patch.object(checks.urllib.request, "build_opener")
    def test_mismatched_or_malformed_project_rejected(self, build_opener):
        for document in ({"id": "other", "visibility": "private"}, {"id": None},
                         {"id": 42}, {}, [], None):
            build_opener.return_value.open.return_value = io.BytesIO(json.dumps(document).encode())
            with self.assertRaisesRegex(ValueError, "project response"):
                checks.verify(environment())

    @mock.patch.object(checks.urllib.request, "build_opener")
    def test_missing_token_and_wrong_environment_fail_without_network(self, build_opener):
        for key, value in (("SYSTEM_ACCESSTOKEN", ""), ("ENVIRONMENT_NAME", "production")):
            env = environment()
            env[key] = value
            with self.assertRaises(ValueError):
                checks.verify(env)
        build_opener.assert_not_called()

    @mock.patch.object(checks.urllib.request, "build_opener")
    def test_api_failure_does_not_expose_token_or_response(self, build_opener):
        build_opener.return_value.open.side_effect = urllib.error.URLError("synthetic-private-response")
        with self.assertRaises(RuntimeError) as error:
            checks.verify(environment())
        self.assertNotIn("synthetic", str(error.exception))

    def test_redirects_rejected(self):
        with self.assertRaises(ValueError):
            checks.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")

    def test_native_gate_and_saved_plan_pipeline_contract_is_retained(self):
        pipeline = (ROOT / "azure-pipelines-create.yml").read_text()
        self.assertEqual(pipeline.count("environment: aks-sandbox"), 2)
        for required in ("lockBehavior: sequential", "task: ManualValidation@1", "pool: server",
                         "onTimeout: reject", "dependsOn: ReviewPlan", "default: false"):
            self.assertIn(required, pipeline)
        template = (ROOT / "pipelines/templates/terraform-job-steps.yml").read_text()
        self.assertIn("Verify private project and sandbox environment context", template)
        self.assertIn("buildType: current", template)
        self.assertIn("artifactName: sandbox-plan", template)


if __name__ == "__main__":
    unittest.main()
