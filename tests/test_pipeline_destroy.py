"""Destroy tests use synthetic state/plans and never contact Azure."""

import contextlib
import copy
import hashlib
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_terraform as runner


SETTINGS = runner.load_config(runner.DEFAULT_CONFIG)
INPUTS = json.loads((runner.ROOT / "terraform/sandbox.auto.tfvars.json").read_text())
PROVIDER = "registry.terraform.io/hashicorp/azurerm"
RG = "azurerm_resource_group.sandbox"
ACR = "azurerm_container_registry.sandbox"
AKS = "azurerm_kubernetes_cluster.sandbox"
ROLE = "azurerm_role_assignment.acr_pull"
PRINCIPAL = "33333333-3333-4333-8333-333333333333"


def state_document(addresses=None):
    base, acr, cluster = runner.sandbox_layout(SETTINGS, INPUTS)
    values = {}
    for address, name, identifier in (
        (RG, INPUTS["resource_group_name"], base),
        (ACR, INPUTS["acr_name"], acr),
        (AKS, INPUTS["cluster_name"], cluster),
    ):
        values[address] = {
            "id": identifier, "name": name, "location": SETTINGS["location"],
            "tags": {"managed-by": "terraform", "environment": SETTINGS["environment"]},
        }
        if address != RG:
            values[address]["resource_group_name"] = INPUTS["resource_group_name"]
    values[AKS].update({
        "node_resource_group": INPUTS["node_resource_group_name"],
        "kubelet_identity": [{"object_id": PRINCIPAL}],
        "kube_config_raw": "synthetic-private-value-never-log",
    })
    values[ROLE] = {
        "id": acr + "/providers/Microsoft.Authorization/roleAssignments/55555555-5555-4555-8555-555555555555",
        "scope": acr, "principal_id": PRINCIPAL, "role_definition_name": "AcrPull",
        "role_definition_id": ("/subscriptions/" + SETTINGS["subscription_id"]
                               + "/providers/Microsoft.Authorization/roleDefinitions/" + runner.ACR_PULL_ROLE),
    }
    selected = set(values) if addresses is None else set(addresses)
    return {"format_version": "1.0", "terraform_version": runner.VERSION, "values": {
        "root_module": {"resources": [
            {"address": address, "mode": "managed", "provider_name": PROVIDER, "values": data}
            for address, data in values.items() if address in selected
        ]},
    }}


def destroy_document(addresses=None):
    state = state_document(addresses)
    return {
        "terraform_version": runner.VERSION,
        "variables": {key: {"value": value} for key, value in INPUTS.items()},
        "prior_state": state,
        "planned_values": {"root_module": {}},
        "resource_changes": [
            {"address": item["address"], "mode": "managed", "provider_name": PROVIDER,
             "change": {"actions": ["delete"], "before": copy.deepcopy(item["values"]), "after": None}}
            for item in state["values"]["root_module"]["resources"]
        ],
    }


def identity():
    return {
        "run_id": "42", "source_commit": "a" * 40, "terraform_version": runner.VERSION,
        "client_id": "11111111-1111-4111-8111-111111111111",
        "service_connection_id": "22222222-2222-4222-8222-222222222222",
    }


def source():
    return {
        "PROVISION_REQUESTED": "false", "DESTROY_REQUESTED": "true",
        "DESTRUCTION_CONFIRMATION": "destroy aks-sandbox",
        "servicePrincipalId": identity()["client_id"], "tenantId": SETTINGS["tenant_id"],
        "AZURESUBSCRIPTION_SERVICE_CONNECTION_ID": identity()["service_connection_id"],
        "SYSTEM_ACCESSTOKEN": "synthetic-token-not-real",
        "SYSTEM_OIDCREQUESTURI": "https://dev.azure.com/example/_apis/oidctoken",
        "TF_RUN_ID": "42", "TF_SOURCE_COMMIT": "a" * 40,
    }


class DestroyStateTests(unittest.TestCase):
    def test_complete_and_every_nonempty_partial_sandbox_inventory(self):
        for size in range(1, 5):
            for addresses in itertools.combinations(sorted(runner.ADDRESSES), size):
                with self.subTest(addresses=addresses):
                    result = runner.validate_destroy_state(state_document(addresses), SETTINGS, INPUTS)
                    self.assertEqual(set(result), set(addresses))

    def test_empty_state_rejected(self):
        for document in ({}, {"values": {}}, state_document([])):
            with self.assertRaisesRegex(ValueError, "state is empty"):
                runner.validate_destroy_state(document, SETTINGS, INPUTS)

    def test_malformed_state_rejected_cleanly(self):
        for document in (
            None, [], {"values": [1]}, {"values": {"root_module": [1]}},
            {"values": {"root_module": {"resources": {"unexpected": True}}}},
            {"values": {"root_module": {"resources": [None]}}},
        ):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    runner.validate_destroy_state(document, SETTINGS, INPUTS)

    def test_unknown_module_address_provider_or_duplicate_rejected(self):
        for mutation in ("module", "address", "provider", "duplicate", "data"):
            with self.subTest(mutation=mutation):
                document = state_document()
                root = document["values"]["root_module"]
                if mutation == "module":
                    root["child_modules"] = [{"resources": []}]
                elif mutation == "address":
                    root["resources"][0]["address"] = "azurerm_resource_group.state_storage"
                elif mutation == "provider":
                    root["resources"][0]["provider_name"] = "registry.terraform.io/other/azurerm"
                elif mutation == "data":
                    root["resources"][0]["mode"] = "data"
                else:
                    root["resources"].append(copy.deepcopy(root["resources"][0]))
                with self.assertRaises(ValueError):
                    runner.validate_destroy_state(document, SETTINGS, INPUTS)

    def test_wrong_identity_scope_tags_or_principal_rejected(self):
        mutations = (
            (RG, "id", "/subscriptions/other/resourceGroups/aks-sandbox-rg"),
            (RG, "name", "aks-tfstate-rg"),
            (RG, "tags", {}),
            (ACR, "resource_group_name", "fitxmatt-rg"),
            (ACR, "location", "westus"),
            (AKS, "node_resource_group", "MC_fitxmatt-rg_fitxmatt-aks_northeurope"),
            (AKS, "kubelet_identity", []),
            (ROLE, "scope", "/subscriptions/other/resourceGroups/fitxmatt-rg"),
            (ROLE, "role_definition_name", "Contributor"),
            (ROLE, "role_definition_id", "/roleDefinitions/other-role"),
            (ROLE, "principal_id", identity()["client_id"]),
            (ROLE, "principal_id", None),
            (ROLE, "id", runner.sandbox_layout(SETTINGS, INPUTS)[1]
             + "/providers/Microsoft.Authorization/roleAssignments/not-a-uuid"),
        )
        for address, key, value in mutations:
            with self.subTest(address=address, key=key):
                document = state_document()
                item = next(r for r in document["values"]["root_module"]["resources"] if r["address"] == address)
                item["values"][key] = value
                with self.assertRaises(ValueError):
                    runner.validate_destroy_state(document, SETTINGS, INPUTS)

    def test_state_storage_group_cannot_be_selected(self):
        inputs = dict(INPUTS, resource_group_name=SETTINGS["resource_group_name"])
        with self.assertRaises(ValueError):
            runner.validate_destroy_state(state_document(), SETTINGS, inputs)


class DestroyPlanTests(unittest.TestCase):
    def test_full_and_partial_delete_only_plan(self):
        for addresses in (None, [RG], [RG, ACR], [ROLE]):
            document = destroy_document(addresses)
            result = runner.validate_destroy_plan(document, SETTINGS, INPUTS)
            self.assertEqual(len(result), len(document["resource_changes"]))

    def test_all_non_delete_actions_rejected(self):
        for actions in (["create"], ["update"], ["no-op"], ["delete", "create"], ["forget"]):
            document = destroy_document()
            document["resource_changes"][0]["change"]["actions"] = actions
            with self.assertRaises(ValueError):
                runner.validate_destroy_plan(document, SETTINGS, INPUTS)

    def test_import_move_after_values_or_wrong_provider_rejected(self):
        for mutation in ("import", "move", "after", "provider"):
            document = destroy_document()
            item = document["resource_changes"][0]
            if mutation == "import":
                item["change"]["importing"] = {"id": "unexpected"}
            elif mutation == "move":
                item["previous_address"] = "old"
            elif mutation == "provider":
                item["provider_name"] = "registry.terraform.io/other/azurerm"
            else:
                item["change"]["after"] = {"id": "unexpected"}
            with self.assertRaises(ValueError):
                runner.validate_destroy_plan(document, SETTINGS, INPUTS)

    def test_empty_extra_missing_or_retained_resources_rejected(self):
        for mutation in ("empty", "extra", "missing", "retained", "module"):
            document = destroy_document()
            if mutation == "empty":
                document["resource_changes"] = []
            elif mutation == "extra":
                item = copy.deepcopy(document["resource_changes"][0])
                item["address"] = "azurerm_resource_group.unrelated"
                document["resource_changes"].append(item)
            elif mutation == "missing":
                document["resource_changes"].pop()
            elif mutation == "module":
                document["planned_values"]["root_module"]["child_modules"] = [{}]
            else:
                document["planned_values"]["root_module"]["resources"] = [{}]
            with self.assertRaises(ValueError):
                runner.validate_destroy_plan(document, SETTINGS, INPUTS)

    def test_inputs_version_prior_state_and_before_values_must_match(self):
        for mutation in ("inputs", "version", "prior", "before"):
            document = destroy_document()
            if mutation == "inputs":
                document["variables"]["node_count"]["value"] = 3
            elif mutation == "version":
                document["terraform_version"] = "1.8.0"
            elif mutation == "prior":
                document["prior_state"] = {}
            else:
                document["resource_changes"][0]["change"]["before"]["unexpected"] = "drift"
            with self.assertRaises(ValueError):
                runner.validate_destroy_plan(document, SETTINGS, INPUTS)

    def test_create_guard_still_rejects_destroy_plan(self):
        with self.assertRaises(ValueError):
            runner.validate_plan(destroy_document(), SETTINGS, INPUTS)


class DestroyCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle = Path(self.directory.name) / "destroy-plan"

    def bundle_with_manifest(self, operation="destroy"):
        self.bundle.mkdir()
        path = self.bundle / runner.PLAN_NAME
        path.write_bytes(b"synthetic destroy plan")
        manifest = dict(identity(), operation=operation, source_sha256=runner.source_digest(),
                        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        (self.bundle / runner.MANIFEST_NAME).write_text(json.dumps(manifest))

    @mock.patch.object(runner, "terraform_call", return_value={})
    def test_empty_state_stops_before_plan_generation(self, call):
        with self.assertRaisesRegex(ValueError, "state is empty"):
            runner.plan(self.bundle, "terraform", {}, SETTINGS, INPUTS, identity(), operation="destroy")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[1], ["show", "-json"])
        self.assertFalse(self.bundle.exists())

    @mock.patch.object(runner, "terraform_call")
    def test_destroy_plan_uses_destroy_flag_and_operation_bound_manifest(self, call):
        def execute(binary, args, env, **kwargs):
            if args == ["show", "-json"]:
                return state_document()
            if args[0] == "plan":
                self.assertIn("-destroy", args)
                Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("-out="))).write_bytes(b"plan")
                return 2
            return destroy_document()
        call.side_effect = execute
        with contextlib.redirect_stdout(io.StringIO()) as output:
            runner.plan(self.bundle, "terraform", {}, SETTINGS, INPUTS, identity(), operation="destroy")
        manifest = json.loads((self.bundle / runner.MANIFEST_NAME).read_text())
        self.assertEqual(manifest["operation"], "destroy")
        self.assertEqual({p.name for p in self.bundle.iterdir()}, {runner.PLAN_NAME, runner.MANIFEST_NAME})
        self.assertNotIn("synthetic-private", output.getvalue())
        self.assertNotIn("synthetic-private", json.dumps(manifest))

    @mock.patch.object(runner, "terraform_call")
    def test_apply_rechecks_state_and_applies_exact_saved_plan(self, call):
        self.bundle_with_manifest()
        call.side_effect = [destroy_document(), state_document(), 0]
        runner.apply(self.bundle, "terraform", {}, SETTINGS, INPUTS, identity(), operation="destroy")
        commands = [item.args[1] for item in call.call_args_list]
        self.assertEqual([item[0] for item in commands], ["show", "show", "apply"])
        self.assertEqual(commands[-1][-1], str(self.bundle / runner.PLAN_NAME))
        self.assertNotIn("-destroy", commands[-1])

    @mock.patch.object(runner, "terraform_call")
    def test_create_and_destroy_artifacts_are_not_interchangeable(self, call):
        for manifest_operation, apply_operation in (("create", "destroy"), ("destroy", "create")):
            with self.subTest(operation=apply_operation):
                if not self.bundle.exists():
                    self.bundle_with_manifest(manifest_operation)
                manifest_path = self.bundle / runner.MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text())
                manifest["operation"] = manifest_operation
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    runner.apply(self.bundle, "terraform", {}, SETTINGS, INPUTS, identity(), operation=apply_operation)
        call.assert_not_called()

    @mock.patch.object(runner, "terraform_call")
    def test_changed_role_id_or_empty_current_state_stops_apply(self, call):
        self.bundle_with_manifest()
        for empty in (False, True):
            current = state_document([]) if empty else state_document()
            if not empty:
                role = next(r for r in current["values"]["root_module"]["resources"] if r["address"] == ROLE)
                role["values"]["id"] = role["values"]["id"].rsplit("/", 1)[0] + "/66666666-6666-4666-8666-666666666666"
            call.reset_mock()
            call.side_effect = [destroy_document(), current]
            with self.assertRaises(ValueError):
                runner.apply(self.bundle, "terraform", {}, SETTINGS, INPUTS, identity(), operation="destroy")
            self.assertFalse(any(c.args[1][0] == "apply" for c in call.call_args_list))

    @mock.patch.object(runner, "terraform_call")
    def test_refresh_can_remove_already_missing_resources_from_destroy_plan(self, call):
        self.bundle_with_manifest()
        call.side_effect = [destroy_document([RG, ACR]), state_document(), 0]
        runner.apply(self.bundle, "terraform", {}, SETTINGS, INPUTS, identity(), operation="destroy")
        self.assertEqual(call.call_args.args[1][0], "apply")


class DestroyConfirmationTests(unittest.TestCase):
    @mock.patch.object(runner.Azure, "call")
    @mock.patch.object(runner, "terraform_call")
    def test_confirmation_request_and_operation_guards_run_before_access(self, terraform, azure):
        cases = [
            ("plan", "destroy", {"DESTRUCTION_CONFIRMATION": ""}),
            ("apply", "destroy", {"DESTROY_REQUESTED": "false"}),
            ("plan", "destroy", {"PROVISION_REQUESTED": "true"}),
            ("apply", "create", {}),
            ("destroy", "destroy", {}),
            ("plan", "unknown", {}),
        ]
        for action, operation, changes in cases:
            with self.subTest(action=action, operation=operation):
                env = dict(source(), **changes)
                with self.assertRaises(ValueError):
                    runner.execute(action, "/tmp/unused-destroy-plan", env, operation=operation)
        terraform.assert_not_called()
        azure.assert_not_called()

    @mock.patch.object(runner, "plan")
    @mock.patch.object(runner.shutil, "which", return_value="/tools/terraform")
    @mock.patch.object(runner, "terraform_call")
    @mock.patch.object(runner.Azure, "call")
    def test_preview_does_not_require_deletion_boolean(self, azure, terraform, which, plan):
        azure.side_effect = [
            {"id": SETTINGS["subscription_id"], "tenantId": SETTINGS["tenant_id"],
             "state": "Enabled", "user": {"name": identity()["client_id"]}},
            *[{"registrationState": "Registered"} for _ in runner.PROVIDERS],
        ]
        terraform.side_effect = [{"terraform_version": runner.VERSION}, 0, 0, 0]
        with contextlib.redirect_stdout(io.StringIO()):
            runner.execute("plan", "/tmp/unused-destroy-plan", dict(source(), DESTROY_REQUESTED="false"),
                           operation="destroy")
        self.assertEqual(plan.call_args.kwargs["operation"], "destroy")

    def test_destroy_pipeline_retains_native_gates_and_preview_defaults(self):
        text = (runner.ROOT / "azure-pipelines-destroy.yml").read_text()
        for fragment in ("trigger: none", "pr: none", "default: false", "lockBehavior: sequential",
                         "task: ManualValidation@1", "onTimeout: reject", "dependsOn: ReviewDestroyPlan",
                         "operation: destroy", "artifactName: sandbox-destroy-plan"):
            self.assertIn(fragment, text)
        self.assertEqual(text.count("environment: aks-sandbox"), 2)


if __name__ == "__main__":
    unittest.main()
