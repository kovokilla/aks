"""Synthetic Terraform plans and mocked executors: never provision cloud resources."""

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_terraform as runner


SETTINGS = runner.load_config(runner.DEFAULT_CONFIG)
INPUTS = json.loads((runner.ROOT / "terraform/sandbox.auto.tfvars.json").read_text())
CLIENT = "11111111-1111-4111-8111-111111111111"
CONNECTION = "22222222-2222-4222-8222-222222222222"


def environment():
    return {
        "servicePrincipalId": CLIENT, "tenantId": SETTINGS["tenant_id"],
        "AZURESUBSCRIPTION_SERVICE_CONNECTION_ID": CONNECTION,
        "SYSTEM_ACCESSTOKEN": "synthetic-oauth-not-a-real-token",
        "SYSTEM_OIDCREQUESTURI": "https://dev.azure.com/example/_apis/oidctoken",
        "TF_RUN_ID": "123", "TF_SOURCE_COMMIT": "a" * 40,
        "PROVISION_REQUESTED": "true", "SANDBOX_CONFIRMATION": "aks-sandbox",
    }


def plan_document():
    names = (
        ("azurerm_resource_group.sandbox", INPUTS["resource_group_name"]),
        ("azurerm_container_registry.sandbox", INPUTS["acr_name"]),
        ("azurerm_kubernetes_cluster.sandbox", INPUTS["cluster_name"]),
    )
    changes = []
    for address, name in names:
        after = {"name": name, "location": SETTINGS["location"]}
        if address != "azurerm_resource_group.sandbox":
            after["resource_group_name"] = INPUTS["resource_group_name"]
        if "kubernetes_cluster" in address:
            after["node_resource_group"] = INPUTS["node_resource_group_name"]
        changes.append({"address": address, "mode": "managed", "change": {
            "actions": ["create"], "before": None, "after": after,
        }})
    role = "azurerm_role_assignment.acr_pull"
    changes.append({"address": role, "mode": "managed", "change": {
        "actions": ["create"], "before": None, "after": {"role_definition_name": "AcrPull"},
        "after_unknown": {"scope": True},
    }})
    return {
        "terraform_version": runner.VERSION,
        "variables": {key: {"value": value} for key, value in INPUTS.items()},
        "resource_changes": changes,
        "configuration": {"root_module": {"resources": [{
            "address": role, "expressions": {"scope": {
                "references": ["azurerm_container_registry.sandbox.id"],
            }},
        }]}},
    }


class AuthenticationTests(unittest.TestCase):
    def test_refreshable_oidc_without_cli_or_static_id_token(self):
        source = environment()
        source.update({"ARM_CLIENT_SECRET": "synthetic", "idToken": "synthetic",
                       "TF_VAR_resource_group_name": "fitxmatt-rg", "TF_CLI_ARGS": "-destroy"})
        env = runner.federated_environment(source, SETTINGS)
        self.assertEqual(env["ARM_USE_OIDC"], "true")
        self.assertEqual(env["ARM_USE_CLI"], "false")
        self.assertEqual(env["ARM_OIDC_AZURE_SERVICE_CONNECTION_ID"], CONNECTION)
        self.assertEqual(env["ARM_OIDC_REQUEST_URL"], source["SYSTEM_OIDCREQUESTURI"])
        for name in ("ARM_CLIENT_SECRET", "ARM_OIDC_TOKEN", "idToken", "TF_CLI_ARGS",
                     "TF_VAR_resource_group_name"):
            self.assertNotIn(name, env)

    def test_invalid_auth_context_rejected(self):
        for key, value in (
            ("tenantId", CLIENT), ("servicePrincipalId", "<placeholder>"),
            ("AZURESUBSCRIPTION_SERVICE_CONNECTION_ID", ""),
            ("SYSTEM_ACCESSTOKEN", "$(System.AccessToken)"),
            ("SYSTEM_OIDCREQUESTURI", "https://attacker.invalid/token"),
            ("servicePrincipalKey", "synthetic"),
        ):
            with self.subTest(key=key):
                source = environment()
                source[key] = value
                with self.assertRaises(ValueError):
                    runner.federated_environment(source, SETTINGS)

    def test_run_binding(self):
        source = environment()
        env = runner.federated_environment(source, SETTINGS)
        self.assertEqual(runner.run_identity(source, env)["run_id"], "123")
        source["TF_SOURCE_COMMIT"] = "$(Build.SourceVersion)"
        with self.assertRaises(ValueError):
            runner.run_identity(source, env)


class PlanGuardTests(unittest.TestCase):
    def test_expected_create_plan(self):
        runner.validate_plan(plan_document(), SETTINGS, INPUTS)

    def test_noop_and_updates_are_allowed_inside_sandbox(self):
        for action in ("no-op", "update"):
            document = plan_document()
            for item in document["resource_changes"]:
                item["change"]["actions"] = [action]
            runner.validate_plan(document, SETTINGS, INPUTS)

    def test_delete_and_replacement_rejected(self):
        for actions in (["delete"], ["delete", "create"], ["create", "delete"], ["forget"]):
            document = plan_document()
            document["resource_changes"][0]["change"]["actions"] = actions
            with self.assertRaises(ValueError):
                runner.validate_plan(document, SETTINGS, INPUTS)

    def test_unexpected_or_missing_resource_rejected(self):
        for mutation in ("extra", "missing"):
            document = plan_document()
            if mutation == "extra":
                extra = copy.deepcopy(document["resource_changes"][0])
                extra["address"] = "azurerm_resource_group.existing"
                document["resource_changes"].append(extra)
            else:
                document["resource_changes"].pop()
            with self.assertRaises(ValueError):
                runner.validate_plan(document, SETTINGS, INPUTS)

    def test_unexpected_names_locations_groups_and_ids_rejected(self):
        for index, key, value in (
            (0, "name", "fitxmatt-rg"), (0, "location", "westus"),
            (0, "id", "/subscriptions/other/resourceGroups/aks-sandbox-rg"),
            (1, "resource_group_name", "fitxmatt-rg"),
            (2, "node_resource_group", "MC_fitxmatt"),
            (3, "scope", "/subscriptions/unrelated"),
            (3, "role_definition_name", "Contributor"),
            (3, "id", "/subscriptions/other/providers/Microsoft.Authorization/roleAssignments/id"),
        ):
            with self.subTest(key=key):
                document = plan_document()
                document["resource_changes"][index]["change"]["after"][key] = value
                with self.assertRaises(ValueError):
                    runner.validate_plan(document, SETTINGS, INPUTS)

    def test_import_or_move_rejected(self):
        document = plan_document()
        document["resource_changes"][0]["change"]["importing"] = {"id": "existing"}
        with self.assertRaises(ValueError):
            runner.validate_plan(document, SETTINGS, INPUTS)
        document = plan_document()
        document["resource_changes"][0]["previous_address"] = "old"
        with self.assertRaises(ValueError):
            runner.validate_plan(document, SETTINGS, INPUTS)

    def test_variable_version_or_unknown_scope_mismatch_rejected(self):
        for mutation in ("variable", "version", "scope"):
            document = plan_document()
            if mutation == "variable":
                document["variables"]["node_count"]["value"] = 3
            elif mutation == "version":
                document["terraform_version"] = "1.8.0"
            else:
                document["configuration"]["root_module"]["resources"][0]["expressions"]["scope"] = {
                    "references": ["azurerm_resource_group.sandbox.id"],
                }
            with self.assertRaises(ValueError):
                runner.validate_plan(document, SETTINGS, INPUTS)


class SavedPlanTests(unittest.TestCase):
    def setUp(self):
        source = environment()
        self.env = runner.federated_environment(source, SETTINGS)
        self.identity = runner.run_identity(source, self.env)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle = Path(self.directory.name) / "plan"

    def create_bundle(self):
        self.bundle.mkdir()
        path = self.bundle / runner.PLAN_NAME
        path.write_bytes(b"synthetic plan")
        manifest = dict(self.identity, operation="create", source_sha256=runner.source_digest(),
                        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        (self.bundle / runner.MANIFEST_NAME).write_text(json.dumps(manifest))

    @mock.patch.object(runner, "terraform_call", return_value=plan_document())
    def test_apply_uses_exact_saved_plan_without_replanning(self, call):
        self.create_bundle()
        runner.apply(self.bundle, "terraform", self.env, SETTINGS, INPUTS, self.identity)
        commands = [c.args[1] for c in call.call_args_list]
        self.assertEqual([c[0] for c in commands], ["show", "apply"])
        self.assertEqual(commands[1][-1], str(self.bundle / runner.PLAN_NAME))

    @mock.patch.object(runner, "terraform_call")
    def test_tampered_plan_source_run_or_commit_rejected(self, call):
        for field in ("plan_sha256", "source_sha256", "run_id", "source_commit", "client_id"):
            with self.subTest(field=field):
                if not self.bundle.exists():
                    self.create_bundle()
                manifest = json.loads((self.bundle / runner.MANIFEST_NAME).read_text())
                original = dict(manifest)
                manifest[field] = "unexpected"
                (self.bundle / runner.MANIFEST_NAME).write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    runner.apply(self.bundle, "terraform", self.env, SETTINGS, INPUTS, self.identity)
                (self.bundle / runner.MANIFEST_NAME).write_text(json.dumps(original))
        call.assert_not_called()

    @mock.patch.object(runner, "terraform_call")
    def test_extra_artifact_or_symlink_rejected(self, call):
        self.create_bundle()
        extra = self.bundle / "state.json"
        extra.write_text("not allowed")
        with self.assertRaises(ValueError):
            runner.apply(self.bundle, "terraform", self.env, SETTINGS, INPUTS, self.identity)
        extra.unlink()
        plan = self.bundle / runner.PLAN_NAME
        plan.unlink()
        plan.symlink_to(self.bundle / runner.MANIFEST_NAME)
        with self.assertRaises(ValueError):
            runner.apply(self.bundle, "terraform", self.env, SETTINGS, INPUTS, self.identity)
        call.assert_not_called()

    @mock.patch.object(runner, "terraform_call")
    def test_plan_publishes_no_json_or_credentials(self, call):
        def execute(binary, args, env, **kwargs):
            if args[0] == "plan":
                path = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("-out=")))
                path.write_bytes(b"synthetic plan")
                return 2
            return plan_document()
        call.side_effect = execute
        runner.plan(self.bundle, "terraform", self.env, SETTINGS, INPUTS, self.identity)
        self.assertEqual({p.name for p in self.bundle.iterdir()}, {runner.PLAN_NAME, runner.MANIFEST_NAME})
        manifest = (self.bundle / runner.MANIFEST_NAME).read_text()
        self.assertNotIn("synthetic-oauth", manifest)
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o700)


class ExecutionTests(unittest.TestCase):
    @mock.patch.object(runner, "plan")
    @mock.patch.object(runner.shutil, "which", return_value="/tools/terraform")
    @mock.patch.object(runner, "terraform_call")
    @mock.patch.object(runner.Azure, "call")
    def test_plan_flow_initializes_remote_backend_then_validates(self, azure, terraform, which, plan):
        azure.side_effect = [
            {"id": SETTINGS["subscription_id"], "tenantId": SETTINGS["tenant_id"],
             "state": "Enabled", "user": {"name": CLIENT}},
            *[{"registrationState": "Registered"} for _ in runner.PROVIDERS],
        ]
        terraform.side_effect = [{"terraform_version": runner.VERSION}, 0, 0, 0]
        with contextlib.redirect_stdout(io.StringIO()):
            runner.execute("plan", "/tmp/unused-plan", environment())
        commands = [call.args[1] for call in terraform.call_args_list]
        self.assertEqual([command[0] for command in commands], ["version", "init", "fmt", "validate"])
        self.assertIn("-lockfile=readonly", commands[1])
        self.assertNotIn("-backend=false", commands[1])
        plan.assert_called_once()

    @mock.patch.object(runner.shutil, "which", return_value="/tools/terraform")
    @mock.patch.object(runner, "terraform_call", return_value={"terraform_version": runner.VERSION})
    @mock.patch.object(runner.Azure, "call", return_value={
        "id": CLIENT, "tenantId": SETTINGS["tenant_id"], "state": "Enabled",
        "user": {"name": CLIENT},
    })
    def test_wrong_subscription_fails_before_backend_init(self, azure, terraform, which):
        with self.assertRaises(ValueError):
            runner.execute("plan", "/tmp/unused-plan", environment())
        self.assertEqual(terraform.call_count, 1)
    @mock.patch.object(runner.Azure, "call")
    @mock.patch.object(runner, "terraform_call")
    def test_apply_confirmation_fails_before_any_access(self, terraform, azure):
        for key, value in (("SANDBOX_CONFIRMATION", "no"), ("PROVISION_REQUESTED", "false")):
            source = environment()
            source[key] = value
            with self.assertRaises(ValueError):
                runner.execute("apply", "/tmp/unused-plan", source)
        terraform.assert_not_called()
        azure.assert_not_called()

    @mock.patch.object(runner.shutil, "which", return_value="/tools/terraform")
    @mock.patch.object(runner, "terraform_call", return_value={"terraform_version": "1.8.0"})
    @mock.patch.object(runner.Azure, "call")
    def test_version_mismatch_fails_before_azure(self, azure, terraform, which):
        with self.assertRaises(ValueError):
            runner.execute("plan", "/tmp/unused-plan", environment())
        azure.assert_not_called()

    @mock.patch.object(runner.shutil, "which", return_value="/tools/terraform")
    @mock.patch.object(runner, "terraform_call", return_value={"terraform_version": runner.VERSION})
    @mock.patch.object(runner.Azure, "call")
    def test_unregistered_provider_fails_before_backend_init(self, azure, terraform, which):
        azure.side_effect = [
            {"id": SETTINGS["subscription_id"], "tenantId": SETTINGS["tenant_id"],
             "state": "Enabled", "user": {"name": CLIENT}},
            {"registrationState": "NotRegistered"},
        ]
        with self.assertRaisesRegex(ValueError, "administrator must register"):
            runner.execute("plan", "/tmp/unused-plan", environment())
        self.assertEqual(terraform.call_count, 1)

    @mock.patch.object(runner.subprocess, "run")
    def test_json_output_is_captured_without_printing(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"synthetic_private": true}', "")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = runner.terraform_call("terraform", ["show", "-json", "plan"], {}, json_output=True)
        self.assertTrue(result["synthetic_private"])
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(run.call_args.kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
