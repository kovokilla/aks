"""Offline unit tests: all Azure calls are faked; no credentials are needed."""

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_state", ROOT / "scripts/bootstrap_state.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
PRINCIPAL = "33333333-3333-4333-8333-333333333333"


def config():
    return {
        "subscription_id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
        "location": "northeurope",
        "environment": "aks-sandbox",
        "resource_group_name": "aks-tfstate-rg",
        "storage_account_name": "teststatestorage123",
        "container_name": "tfstate",
        "key": "sandbox/terraform.tfstate",
    }


class FakeAzure:
    def __init__(self, settings, existing=False):
        self.settings = settings
        self.calls = []
        self.account = {"id": settings["subscription_id"], "tenantId": settings["tenant_id"],
                        "state": "Enabled"}
        self.group_exists = existing
        self.group = {"location": settings["location"], "tags": {
            "managed-by": bootstrap.OWNER_TAG, "environment": settings["environment"]}}
        self.storage = dict(copy.deepcopy(self.group), name=settings["storage_account_name"],
                            kind="StorageV2", sku={"name": "Standard_LRS"})
        self.accounts = [self.storage] if existing else []
        self.containers = [{"name": settings["container_name"], "metadata": {
            "managed_by": bootstrap.OWNER_TAG, "environment": settings["environment"]}}] if existing else []
        self.assignments = []
        self.name_available = True

    def call(self, *args):
        self.calls.append(args)
        if args[:2] == ("account", "show"):
            return self.account
        if args[:2] == ("group", "exists"):
            return self.group_exists
        if args[:2] == ("group", "show"):
            return self.group
        if args[:3] == ("storage", "account", "list"):
            return self.accounts
        if args[:3] == ("storage", "account", "check-name"):
            return {"nameAvailable": self.name_available}
        if args[:3] == ("storage", "container-rm", "list"):
            # Azure's list response can omit metadata even when show returns it.
            return [{"name": item["name"]} for item in self.containers]
        if args[:3] == ("storage", "container-rm", "show"):
            name = args[args.index("--name") + 1]
            return next(item for item in self.containers if item["name"] == name)
        if args[:3] == ("role", "assignment", "list"):
            if "--scope" in args and "--all" in args:
                raise ValueError("Azure CLI does not allow --all with --scope.")
            return self.assignments
        return {}

    def mutations(self):
        return [call for call in self.calls if "create" in call or "update" in call]


class ConfigTests(unittest.TestCase):
    def load(self, settings):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(settings))
            return bootstrap.load_config(path)

    def test_repository_config_valid(self):
        self.assertEqual(set(bootstrap.load_config(bootstrap.DEFAULT_CONFIG)), bootstrap.FIELDS)

    def test_invalid_config_rejected(self):
        for field, value in (
            ("subscription_id", "<SUBSCRIPTION_ID>"),
            ("tenant_id", "00000000-0000-0000-0000-000000000000"),
            ("storage_account_name", "Bad-Name"),
            ("container_name", "bad--container"),
            ("key", "../state"),
            ("key", "/state"),
            ("key", "sandbox//state"),
            ("location", "North Europe"),
            ("environment", ""),
        ):
            with self.subTest(field=field, value=value):
                settings = config()
                settings[field] = value
                with self.assertRaises(ValueError):
                    self.load(settings)

    def test_extra_field_rejected(self):
        settings = config()
        settings["access_key"] = "not-a-real-key"
        with self.assertRaises(ValueError):
            self.load(settings)

    def test_missing_field_rejected(self):
        settings = config()
        del settings["tenant_id"]
        with self.assertRaises(ValueError):
            self.load(settings)

    def test_backend_hcl_contains_no_credentials(self):
        hcl = bootstrap.backend_hcl(config())
        self.assertIn('key = "sandbox/terraform.tfstate"', hcl)
        self.assertIn("use_azuread_auth = true", hcl)
        self.assertIn("use_oidc = true", hcl)
        for credential in ("access_key", "client_secret", "sas_token", "oidc_token"):
            self.assertNotIn(credential, hcl)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.azure = FakeAzure(self.config)

    def test_new_storage_secure_and_container_scoped_role(self):
        scope = bootstrap.bootstrap(self.config, self.azure, PRINCIPAL)
        storage = next(c for c in self.azure.calls if c[:3] == ("storage", "account", "create"))
        for flag, value in (
            ("--https-only", "true"), ("--min-tls-version", "TLS1_2"),
            ("--allow-blob-public-access", "false"), ("--allow-shared-key-access", "false"),
        ):
            self.assertEqual(storage[storage.index(flag) + 1], value)
        protection = next(c for c in self.azure.calls if "blob-service-properties" in c)
        self.assertEqual(protection[protection.index("--enable-versioning") + 1], "true")
        self.assertEqual(protection[protection.index("--delete-retention-days") + 1], "14")
        self.assertEqual(protection[protection.index("--container-delete-retention-days") + 1], "14")
        container = next(c for c in self.azure.calls if c[:3] == ("storage", "container-rm", "create"))
        self.assertIn("--fail-on-exist", container)
        self.assertEqual(container[container.index("--public-access") + 1], "off")
        role = next(c for c in self.azure.calls if c[:3] == ("role", "assignment", "create"))
        self.assertEqual(role[role.index("--scope") + 1], scope)
        self.assertTrue(scope.endswith("/blobServices/default/containers/tfstate"))
        self.assertEqual(role[role.index("--assignee-object-id") + 1], PRINCIPAL)
        self.assertEqual(role[role.index("--role") + 1], bootstrap.BLOB_CONTRIBUTOR)
        self.assertFalse(any("delete" in c or "get-access-token" in c for c in self.azure.calls))

    def test_mismatched_subscription_tenant_or_disabled_account_fails_before_mutation(self):
        for field, value in (("id", PRINCIPAL), ("tenantId", PRINCIPAL), ("state", "Disabled")):
            with self.subTest(field=field):
                azure = FakeAzure(self.config)
                azure.account[field] = value
                with self.assertRaises(ValueError):
                    bootstrap.bootstrap(self.config, azure, PRINCIPAL)
                self.assertEqual(azure.mutations(), [])

    def test_invalid_principal_rejected_before_azure(self):
        with self.assertRaises(ValueError):
            bootstrap.bootstrap(self.config, self.azure, "not-an-object-id")
        self.assertEqual(self.azure.calls, [])

    def test_global_name_collision_fails_before_group_creation(self):
        self.azure.name_available = False
        with self.assertRaisesRegex(ValueError, "unavailable"):
            bootstrap.bootstrap(self.config, self.azure, PRINCIPAL)
        self.assertEqual(self.azure.mutations(), [])

    def test_unrelated_group_or_storage_rejected(self):
        for target in ("group", "storage"):
            with self.subTest(target=target):
                azure = FakeAzure(self.config, existing=True)
                getattr(azure, target)["tags"] = {}
                with self.assertRaisesRegex(ValueError, "unrelated"):
                    bootstrap.bootstrap(self.config, azure, PRINCIPAL)
                self.assertEqual(azure.mutations(), [])

    def test_unrelated_container_rejected(self):
        azure = FakeAzure(self.config, existing=True)
        azure.containers[0]["metadata"] = {}
        with self.assertRaisesRegex(ValueError, "unrelated existing container"):
            bootstrap.bootstrap(self.config, azure, PRINCIPAL)
        self.assertEqual(azure.mutations(), [])

    def test_incompatible_account_rejected(self):
        for field, value in (("location", "westus"), ("kind", "BlobStorage"),
                             ("sku", {"name": "Premium_LRS"}), ("isHnsEnabled", True)):
            with self.subTest(field=field):
                azure = FakeAzure(self.config, existing=True)
                azure.storage[field] = value
                with self.assertRaises(ValueError):
                    bootstrap.bootstrap(self.config, azure, PRINCIPAL)
                self.assertEqual(azure.mutations(), [])

    def test_rerun_hardens_owned_resources_without_duplicate_role(self):
        azure = FakeAzure(self.config, existing=True)
        scope = bootstrap.bootstrap(self.config, azure, PRINCIPAL)
        azure.calls.clear()
        azure.assignments = [{"principalId": PRINCIPAL, "scope": scope,
                              "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/"
                              + bootstrap.BLOB_CONTRIBUTOR}]
        bootstrap.bootstrap(self.config, azure, PRINCIPAL)
        self.assertFalse(any("create" in c for c in azure.calls))
        self.assertTrue(any("update" in c for c in azure.calls))

    def test_container_metadata_is_read_from_full_resource(self):
        azure = FakeAzure(self.config, existing=True)
        bootstrap.bootstrap(self.config, azure, PRINCIPAL)
        shows = [c for c in azure.calls if c[:3] == ("storage", "container-rm", "show")]
        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0][shows[0].index("--name") + 1], self.config["container_name"])

    def test_partial_bootstrap_can_resume(self):
        azure = FakeAzure(self.config, existing=True)
        azure.containers = []
        bootstrap.bootstrap(self.config, azure, PRINCIPAL)
        creates = [c[:3] for c in azure.calls if "create" in c]
        self.assertEqual(creates, [("storage", "container-rm", "create"), ("role", "assignment", "create")])

    def test_role_assignment_name_is_deterministic(self):
        other = FakeAzure(self.config)
        bootstrap.bootstrap(self.config, self.azure, PRINCIPAL)
        bootstrap.bootstrap(self.config, other, PRINCIPAL)
        names = []
        for azure in (self.azure, other):
            role = next(c for c in azure.calls if c[:3] == ("role", "assignment", "create"))
            names.append(role[role.index("--name") + 1])
        self.assertEqual(names[0], names[1])

    def test_role_lookup_is_scoped_and_skips_graph_name_lookup(self):
        scope = bootstrap.bootstrap(self.config, self.azure, PRINCIPAL)
        lookup = next(c for c in self.azure.calls if c[:3] == ("role", "assignment", "list"))
        self.assertEqual(lookup[lookup.index("--scope") + 1], scope)
        self.assertNotIn("--all", lookup)
        self.assertEqual(lookup[lookup.index("--fill-principal-name") + 1], "false")

    def test_read_error_is_not_treated_as_missing_resource(self):
        self.azure.call = mock.Mock(side_effect=RuntimeError("Azure unavailable"))
        with self.assertRaises(RuntimeError):
            bootstrap.bootstrap(self.config, self.azure, PRINCIPAL)
        self.assertEqual(self.azure.call.call_count, 1)


class CommandTests(unittest.TestCase):
    def run_main(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bootstrap.main(args)

    @mock.patch.object(bootstrap.Azure, "call", side_effect=AssertionError("No Azure calls allowed"))
    def test_preview_and_config_render_are_offline(self, call):
        self.assertEqual(self.run_main([]), 0)
        self.assertEqual(self.run_main(["--print-backend-config"]), 0)
        call.assert_not_called()

    @mock.patch.object(bootstrap.Azure, "call", side_effect=AssertionError("No Azure calls allowed"))
    def test_apply_requires_confirmation_and_principal(self, call):
        self.assertEqual(self.run_main(["--apply"]), 1)
        subscription = bootstrap.load_config(bootstrap.DEFAULT_CONFIG)["subscription_id"]
        self.assertEqual(self.run_main(["--apply", "--confirm-subscription", subscription]), 1)
        call.assert_not_called()

    @mock.patch.object(bootstrap.shutil, "which", return_value=None)
    def test_apply_requires_cli(self, which):
        subscription = bootstrap.load_config(bootstrap.DEFAULT_CONFIG)["subscription_id"]
        self.assertEqual(self.run_main([
            "--apply", "--confirm-subscription", subscription,
            "--pipeline-principal-object-id", PRINCIPAL,
        ]), 1)

    @mock.patch.object(bootstrap.subprocess, "run")
    def test_cli_pins_subscription_and_does_not_invoke_shell(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"id": "example"}', "")
        self.assertEqual(bootstrap.Azure("test-subscription").call("account", "show"), {"id": "example"})
        args, kwargs = run.call_args
        self.assertIn("--subscription", args[0])
        self.assertEqual(args[0][args[0].index("--subscription") + 1], "test-subscription")
        self.assertFalse(kwargs.get("shell", False))

    @mock.patch.object(bootstrap.subprocess, "run")
    def test_cli_failure_preserves_known_codes_without_raw_response(self, run):
        for code in bootstrap.AZURE_ERROR_HINTS:
            with self.subTest(code=code):
                run.return_value = subprocess.CompletedProcess(
                    [], 1, "private stdout", f"ERROR: ({code}) private details",
                )
                with self.assertRaises(RuntimeError) as error:
                    bootstrap.Azure("test-subscription").call("role", "assignment", "create")
                self.assertIn(code, str(error.exception))
                self.assertNotIn("private", str(error.exception))

    @mock.patch.object(bootstrap.subprocess, "run")
    def test_cli_failure_does_not_dump_responses(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "private stdout", "private stderr")
        with self.assertRaises(RuntimeError) as error:
            bootstrap.Azure("test-subscription").call("account", "show")
        self.assertNotIn("private", str(error.exception))


if __name__ == "__main__":
    unittest.main()
