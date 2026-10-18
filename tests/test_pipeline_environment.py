"""Azure DevOps API checks are tested with synthetic responses; no network."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_pipeline_environment as checks


def environment():
    return {
        "SYSTEM_COLLECTIONURI": "https://dev.azure.com/example/",
        "SYSTEM_TEAMPROJECTID": "11111111-1111-4111-8111-111111111111",
        "ENVIRONMENT_ID": "42", "ENVIRONMENT_NAME": "aks-sandbox",
        "SYSTEM_ACCESSTOKEN": "synthetic-oauth-not-a-real-token",
    }


def configuration():
    return {"value": [
        {"resource": {"type": "environment", "id": "42"},
         "type": {"name": "Approval"}, "isDisabled": False, "issue": None},
        {"resource": {"type": "environment", "id": "42"},
         "type": {"name": "ExclusiveLock"}, "isDisabled": False},
    ]}


class EnvironmentChecksTests(unittest.TestCase):
    def test_expected_checks(self):
        checks.require_checks(configuration(), "42")

    def test_missing_disabled_wrong_resource_and_error_checks_fail(self):
        for mutation in ("missing", "disabled", "wrong_resource", "issue"):
            with self.subTest(mutation=mutation):
                data = configuration()
                if mutation == "missing":
                    data["value"].pop()
                elif mutation == "disabled":
                    data["value"][1]["isDisabled"] = True
                elif mutation == "wrong_resource":
                    data["value"][1]["resource"]["id"] = "99"
                else:
                    data["value"][1]["issue"] = {"type": "error"}
                with self.assertRaises(ValueError):
                    checks.require_checks(data, "42")

    def test_official_scoped_endpoint(self):
        url = checks.checks_url(environment())
        self.assertTrue(url.startswith("https://dev.azure.com/example/"))
        self.assertIn("resourceType=environment", url)
        self.assertIn("resourceId=42", url)

    def test_untrusted_url_or_bad_environment_id_rejected(self):
        for key, value in (
            ("SYSTEM_COLLECTIONURI", "https://attacker.invalid/"),
            ("SYSTEM_COLLECTIONURI", "http://dev.azure.com/example/"),
            ("ENVIRONMENT_ID", "$(Environment.Id)"),
        ):
            env = environment()
            env[key] = value
            with self.assertRaises(ValueError):
                checks.checks_url(env)

    @mock.patch.object(checks.urllib.request, "build_opener")
    def test_read_only_verification(self, build_opener):
        build_opener.return_value.open.return_value = io.BytesIO(json.dumps(configuration()).encode())
        checks.verify(environment())
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")

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


if __name__ == "__main__":
    unittest.main()
