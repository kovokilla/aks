#!/usr/bin/env python3
"""Azure DevOps OIDC lifecycle runner; destructive plans require separate safeguards."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

from bootstrap_state import Azure, DEFAULT_CONFIG, backend_hcl, load_config, validate_uuid
from install_terraform import VERSION


ROOT = Path(__file__).resolve().parents[1]
PLAN_NAME = "sandbox.tfplan"
MANIFEST_NAME = "manifest.json"
OPERATIONS = ("create", "destroy")
ACR_PULL_ROLE = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
ADDRESSES = {
    "azurerm_resource_group.sandbox",
    "azurerm_container_registry.sandbox",
    "azurerm_kubernetes_cluster.sandbox",
    "azurerm_role_assignment.acr_pull",
}
PROVIDERS = (
    "Microsoft.ContainerService", "Microsoft.ContainerRegistry", "Microsoft.Compute",
    "Microsoft.Network", "Microsoft.ManagedIdentity",
)


def federated_environment(source, settings):
    client = validate_uuid(source.get("servicePrincipalId"))
    tenant = validate_uuid(source.get("tenantId"))
    connection = validate_uuid(source.get("AZURESUBSCRIPTION_SERVICE_CONNECTION_ID"))
    if tenant != settings["tenant_id"] or source.get("servicePrincipalKey"):
        raise ValueError("Use a workload-federated Azure Resource Manager connection in the expected tenant.")
    token = source.get("SYSTEM_ACCESSTOKEN", "")
    endpoint = source.get("SYSTEM_OIDCREQUESTURI", "")
    host = urllib.parse.urlsplit(endpoint)
    if (not token or token.startswith("$(") or host.scheme != "https"
            or not (host.netloc == "dev.azure.com" or host.netloc.endswith(".dev.azure.com"))):
        raise ValueError("The task must supply System.AccessToken and System.OidcRequestUri.")
    env = dict(source)
    for name in list(env):
        if (name.startswith(("ARM_", "TF_CLI_ARGS", "TF_VAR_", "TF_LOG"))
                or name in ("TF_WORKSPACE", "TF_DATA_DIR", "idToken", "servicePrincipalKey")):
            env.pop(name)
    env.update({
        "ARM_USE_OIDC": "true", "ARM_USE_AZUREAD": "true",
        "ARM_USE_CLI": "false", "ARM_USE_MSI": "false",
        "ARM_CLIENT_ID": client, "ARM_TENANT_ID": tenant,
        "ARM_SUBSCRIPTION_ID": settings["subscription_id"],
        # Both pinned Terraform and AzureRM support this refreshable ADO flow.
        "ARM_OIDC_AZURE_SERVICE_CONNECTION_ID": connection,
        "ARM_OIDC_REQUEST_TOKEN": token, "ARM_OIDC_REQUEST_URL": endpoint,
        "TF_INPUT": "0", "TF_IN_AUTOMATION": "true", "TF_WORKSPACE": "default",
    })
    return env


def run_identity(source, env):
    run_id = source.get("TF_RUN_ID", "")
    commit = source.get("TF_SOURCE_COMMIT", "")
    if not run_id.isdigit() or not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Expected the current pipeline Build.BuildId and Build.SourceVersion.")
    return {
        "run_id": run_id, "source_commit": commit,
        "client_id": env["ARM_CLIENT_ID"],
        "service_connection_id": env["ARM_OIDC_AZURE_SERVICE_CONNECTION_ID"],
        "terraform_version": VERSION,
    }


def source_digest():
    paths = set((ROOT / "terraform").glob("*.tf"))
    paths.update((ROOT / "terraform").glob("*.auto.tfvars.json"))
    paths.add(ROOT / "terraform" / ".terraform.lock.hcl")
    paths.add(DEFAULT_CONFIG)
    paths.update((ROOT / "scripts").glob("*.py"))
    paths.update(ROOT.glob("azure-pipelines-*.yml"))
    paths.update((ROOT / "pipelines").rglob("*.yml"))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def same_id(actual, expected):
    return (isinstance(actual, str) and isinstance(expected, str)
            and actual.rstrip("/").lower() == expected.lower())


def validate_plan_header(document, inputs):
    if document.get("terraform_version") != VERSION or document.get("errored", False):
        raise ValueError("Plan version mismatch or errored Terraform plan.")
    planned_inputs = {key: value["value"] for key, value in document.get("variables", {}).items()}
    if planned_inputs != inputs:
        raise ValueError("Plan variables differ from the committed sandbox inputs.")


def sandbox_layout(settings, inputs):
    names = ("resource_group_name", "node_resource_group_name", "cluster_name", "acr_name")
    if not all(isinstance(inputs.get(name), str) for name in names):
        raise ValueError("Sandbox resource names must be strings.")
    base = f"/subscriptions/{settings['subscription_id']}/resourceGroups/{inputs['resource_group_name']}"
    acr = base + "/providers/Microsoft.ContainerRegistry/registries/" + inputs["acr_name"]
    cluster = base + "/providers/Microsoft.ContainerService/managedClusters/" + inputs["cluster_name"]
    if (not inputs["resource_group_name"].startswith("aks-sandbox-")
            or not inputs["node_resource_group_name"].startswith("aks-sandbox-")
            or not inputs["cluster_name"].startswith("aks-sandbox")
            or not inputs["acr_name"].startswith("akssandbox")
            or len({inputs["resource_group_name"], inputs["node_resource_group_name"],
                    settings["resource_group_name"]}) != 3):
        raise ValueError("Sandbox names collide with state storage or leave the sandbox namespace.")
    return base, acr, cluster


def validate_plan(document, settings, inputs):
    """Create mode never permits deletes, imports, moves, or replacements."""
    validate_plan_header(document, inputs)
    changes = document.get("resource_changes", [])
    if {item["address"] for item in changes} != ADDRESSES or len(changes) != len(ADDRESSES):
        raise ValueError("Plan must contain only the four declared sandbox resources.")
    base, acr, cluster = sandbox_layout(settings, inputs)
    expected_names = {
        "azurerm_resource_group.sandbox": (inputs["resource_group_name"], base),
        "azurerm_container_registry.sandbox": (inputs["acr_name"], acr),
        "azurerm_kubernetes_cluster.sandbox": (inputs["cluster_name"], cluster),
    }
    for item in changes:
        change = item["change"]
        if (item.get("mode") != "managed" or item.get("previous_address")
                or change.get("importing") or change["actions"] not in (["create"], ["update"], ["no-op"])):
            raise ValueError("Create pipeline refuses imports, moves, deletion, or replacement.")
        after = change.get("after") or {}
        before = change.get("before") or {}
        if item["address"] in expected_names:
            name, identifier = expected_names[item["address"]]
            if after.get("name") != name:
                raise ValueError("Plan changes an unexpected Azure resource name.")
            if after.get("location") != settings["location"]:
                raise ValueError("Plan changes an unexpected Azure location.")
            if (item["address"] != "azurerm_resource_group.sandbox"
                    and after.get("resource_group_name") != inputs["resource_group_name"]):
                raise ValueError("Plan targets an unexpected resource group.")
            for values in (before, after):
                if values.get("id") and not same_id(values["id"], identifier):
                    raise ValueError("Plan references a resource outside the intended sandbox.")
            if (item["address"] == "azurerm_kubernetes_cluster.sandbox"
                    and after.get("node_resource_group") != inputs["node_resource_group_name"]):
                raise ValueError("Plan targets an unexpected AKS node resource group.")
        else:
            for values in (before, after):
                if values.get("scope") and not same_id(values["scope"], acr):
                    raise ValueError("Role assignment is not scoped to the sandbox ACR.")
                if values.get("id") and not values["id"].lower().startswith(
                        acr.lower() + "/providers/microsoft.authorization/roleassignments/"):
                    raise ValueError("Role assignment ID is outside the sandbox ACR.")
                if values.get("role_definition_name") not in (None, "AcrPull"):
                    raise ValueError("Only the AcrPull role is permitted.")
            if after.get("role_definition_name") != "AcrPull":
                raise ValueError("Expected an AcrPull role assignment.")
            if not after.get("scope"):
                resources = document.get("configuration", {}).get("root_module", {}).get("resources", [])
                role = next((r for r in resources if r.get("address") == item["address"]), {})
                references = role.get("expressions", {}).get("scope", {}).get("references", [])
                allowed = {"azurerm_container_registry.sandbox", "azurerm_container_registry.sandbox.id"}
                if (not change.get("after_unknown", {}).get("scope")
                        or not references or not set(references).issubset(allowed)):
                    raise ValueError("Unknown role scope must reference the sandbox ACR directly.")


def validate_destroy_resource(address, values, settings, inputs):
    """Check real state identities, not merely Terraform address labels."""
    if not isinstance(values, dict):
        raise ValueError("Destroy state resource values must be an object.")
    base, acr, cluster = sandbox_layout(settings, inputs)
    expected = {
        "azurerm_resource_group.sandbox": (inputs["resource_group_name"], base),
        "azurerm_container_registry.sandbox": (inputs["acr_name"], acr),
        "azurerm_kubernetes_cluster.sandbox": (inputs["cluster_name"], cluster),
    }
    if address in expected:
        name, identifier = expected[address]
        if not same_id(values.get("id"), identifier) or values.get("name") != name:
            raise ValueError("Destroy state references an unexpected Azure resource identity.")
        if values.get("location") != settings["location"]:
            raise ValueError("Destroy resource location does not match the sandbox.")
        if address != "azurerm_resource_group.sandbox" and values.get("resource_group_name") != inputs["resource_group_name"]:
            raise ValueError("Destroy resource is outside the sandbox group.")
        tags = values.get("tags") or {}
        if (not isinstance(tags, dict) or tags.get("managed-by") != "terraform"
                or tags.get("environment") != settings["environment"]):
            raise ValueError("Destroy requires matching Terraform ownership/environment tags.")
        if address == "azurerm_kubernetes_cluster.sandbox" and values.get("node_resource_group") != inputs["node_resource_group_name"]:
            raise ValueError("Destroy cluster has an unexpected managed node group.")
    elif address == "azurerm_role_assignment.acr_pull":
        prefix = acr.lower() + "/providers/microsoft.authorization/roleassignments/"
        identifier = values.get("id", "")
        if (not isinstance(identifier, str) or not identifier.lower().startswith(prefix)
                or not same_id(values.get("scope"), acr)):
            raise ValueError("Destroy role assignment must be scoped to the sandbox ACR.")
        validate_uuid(identifier[len(prefix):])
        validate_uuid(values.get("principal_id"))
        if values.get("role_definition_name") != "AcrPull":
            raise ValueError("Destroy may remove only the sandbox AcrPull assignment.")
        role_id = values.get("role_definition_id")
        if role_id and (not isinstance(role_id, str) or not role_id.lower().endswith("/" + ACR_PULL_ROLE)):
            raise ValueError("Destroy role definition ID does not match AcrPull.")
    else:
        raise ValueError("Destroy refuses a resource outside the sandbox allowlist.")


def validate_destroy_state(document, settings, inputs):
    if not isinstance(document, dict):
        raise ValueError("Destroy state must be a JSON object.")
    values = document.get("values") or {}
    if not isinstance(values, dict):
        raise ValueError("Destroy state values must be a JSON object.")
    root = values.get("root_module") or {}
    if not isinstance(root, dict):
        raise ValueError("Destroy state root module must be a JSON object.")
    if root.get("child_modules"):
        raise ValueError("Destroy refuses unexpected modules in the state.")
    resources = root.get("resources") or []
    if not resources:
        raise ValueError("Refusing destroy: state is empty. A plan-only create run does not provision resources.")
    if not isinstance(resources, list):
        raise ValueError("Destroy state resources must be an array.")
    inventory = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise ValueError("Destroy state resource must be an object.")
        address = resource.get("address")
        if (resource.get("mode") != "managed" or address not in ADDRESSES or address in inventory
                or resource.get("provider_name") != "registry.terraform.io/hashicorp/azurerm"):
            raise ValueError("Destroy state contains unexpected or duplicate resource addresses.")
        values = resource.get("values") or {}
        validate_destroy_resource(address, values, settings, inputs)
        inventory[address] = values
    cluster = inventory.get("azurerm_kubernetes_cluster.sandbox")
    role = inventory.get("azurerm_role_assignment.acr_pull")
    if cluster and role:
        kubelets = cluster.get("kubelet_identity") or []
        if (not isinstance(kubelets, list) or len(kubelets) != 1 or not isinstance(kubelets[0], dict)
                or not same_id(role.get("principal_id"), kubelets[0].get("object_id", ""))):
            raise ValueError("Destroy AcrPull principal does not match the sandbox kubelet identity.")
    return inventory


def validate_destroy_plan(document, settings, inputs):
    validate_plan_header(document, inputs)
    prior = validate_destroy_state(document.get("prior_state") or {}, settings, inputs)
    changes = document.get("resource_changes") or []
    if not changes or {item["address"] for item in changes} != set(prior) or len(changes) != len(prior):
        raise ValueError("Destroy plan must delete exactly its nonempty refreshed sandbox inventory.")
    planned = (document.get("planned_values") or {}).get("root_module") or {}
    if planned.get("resources") or planned.get("child_modules"):
        raise ValueError("Destroy plan must not retain or create managed resources.")
    for item in changes:
        change = item["change"]
        if (item.get("mode") != "managed" or item.get("previous_address")
                or item.get("provider_name") != "registry.terraform.io/hashicorp/azurerm"
                or change.get("importing") or change["actions"] != ["delete"]
                or change.get("after") is not None):
            raise ValueError("Destroy plan permits deletes only, not creates, updates, moves, imports, or replacements.")
        before = change.get("before") or {}
        validate_destroy_resource(item["address"], before, settings, inputs)
        if before != prior[item["address"]]:
            raise ValueError("Destroy plan and refreshed state disagree about the resource being deleted.")
    return prior


def terraform_call(binary, args, env, json_output=False, accepted=(0,)):
    command = [binary, f"-chdir={ROOT / 'terraform'}", *args]
    result = subprocess.run(command, env=env, check=False, text=True,
                            capture_output=json_output)
    if result.returncode not in accepted:
        raise RuntimeError(f"Terraform {args[0]} failed (exit {result.returncode}).")
    # show -json can contain secrets: retain it in memory, never log or publish it.
    return json.loads(result.stdout) if json_output else result.returncode


def plan(bundle, binary, env, settings, inputs, identity, operation="create"):
    if operation not in OPERATIONS:
        raise ValueError("Unknown Terraform lifecycle operation.")
    if operation == "destroy":
        state = terraform_call(binary, ["show", "-json"], env, json_output=True)
        validate_destroy_state(state, settings, inputs)
    bundle.mkdir(parents=True, exist_ok=False, mode=0o700)
    plan_path = bundle / PLAN_NAME
    arguments = ["plan", "-input=false", "-lock-timeout=5m",
                 "-detailed-exitcode", f"-out={plan_path}"]
    if operation == "destroy":
        arguments.append("-destroy")
    terraform_call(binary, arguments, env, accepted=(0, 2))
    plan_path.chmod(0o600)
    document = terraform_call(binary, ["show", "-json", str(plan_path)], env, json_output=True)
    validator = validate_destroy_plan if operation == "destroy" else validate_plan
    validator(document, settings, inputs)
    manifest = dict(identity, operation=operation, source_sha256=source_digest(),
                    plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest())
    path = bundle / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    path.chmod(0o600)
    label = "destroy " if operation == "destroy" else ""
    print(f"Sandbox-only {label}plan validated; review the Terraform plan log before approving.")


def apply(bundle, binary, env, settings, inputs, identity, operation="create"):
    if operation not in OPERATIONS:
        raise ValueError("Unknown Terraform lifecycle operation.")
    if {p.name for p in bundle.iterdir()} != {PLAN_NAME, MANIFEST_NAME}:
        raise ValueError("Plan artifact must contain exactly its binary plan and manifest.")
    if any(p.is_symlink() or not p.is_file() for p in bundle.iterdir()):
        raise ValueError("Plan artifact must contain regular files, not links.")
    plan_path = bundle / PLAN_NAME
    manifest = json.loads((bundle / MANIFEST_NAME).read_text())
    expected = dict(identity, operation=operation, source_sha256=source_digest(),
                    plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest())
    if manifest != expected:
        raise ValueError("Plan hash, source, run, commit, version, or pipeline identity mismatch.")
    document = terraform_call(binary, ["show", "-json", str(plan_path)], env, json_output=True)
    validator = validate_destroy_plan if operation == "destroy" else validate_plan
    planned = validator(document, settings, inputs)
    if operation == "destroy":
        state = terraform_call(binary, ["show", "-json"], env, json_output=True)
        current = validate_destroy_state(state, settings, inputs)
        for address, values in planned.items():
            if address not in current or not same_id(current[address].get("id"), values["id"]):
                raise ValueError("Current sandbox inventory no longer matches the reviewed destroy plan.")
    terraform_call(binary, ["apply", "-input=false", "-lock-timeout=5m", str(plan_path)], env)


def execute(action, bundle, source, operation="create"):
    if action not in ("plan", "apply") or operation not in OPERATIONS:
        raise ValueError("Unknown Terraform lifecycle action or operation.")
    settings = load_config(DEFAULT_CONFIG)
    inputs = json.loads((ROOT / "terraform" / "sandbox.auto.tfvars.json").read_text())
    provisioning = source.get("PROVISION_REQUESTED", "false").lower() == "true"
    destroying = source.get("DESTROY_REQUESTED", "false").lower() == "true"
    if operation == "destroy":
        if provisioning or source.get("DESTRUCTION_CONFIRMATION") != f"destroy {settings['environment']}":
            raise ValueError("Destroy preview/apply requires the exact phrase: destroy " + settings["environment"])
        if action == "apply" and not destroying:
            raise ValueError("Destroy apply requires an explicit deletion request.")
    else:
        if destroying:
            raise ValueError("Create mode cannot accept a deletion request.")
        if (action == "apply" or provisioning) and source.get("SANDBOX_CONFIRMATION") != settings["environment"]:
            raise ValueError("Provisioning requires the exact sandbox environment confirmation.")
        if action == "apply" and not provisioning:
            raise ValueError("Apply requires an explicit provisioning request.")
    bundle = Path(bundle).resolve()
    if bundle == ROOT or ROOT in bundle.parents:
        raise ValueError("Keep plan artifacts outside the source checkout.")
    env = federated_environment(source, settings)
    identity = run_identity(source, env)
    binary = shutil.which("terraform")
    if not binary:
        raise ValueError("Install pinned Terraform before running this task.")
    version = terraform_call(binary, ["version", "-json"], env, json_output=True)
    if version["terraform_version"] != VERSION:
        raise ValueError(f"Expected Terraform {VERSION}; found another version.")
    print(f"Terraform version check passed: {VERSION}")
    account = Azure(settings["subscription_id"]).call("account", "show")
    if (account["id"].lower() != settings["subscription_id"]
            or account["tenantId"].lower() != settings["tenant_id"]
            or account.get("state") != "Enabled"
            or account.get("user", {}).get("name", "").lower() != env["ARM_CLIENT_ID"]):
        raise ValueError("Azure CLI service connection does not match the expected subscription/tenant/client.")
    for namespace in PROVIDERS:
        registration = Azure(settings["subscription_id"]).call("provider", "show", "--namespace", namespace)
        if registration.get("registrationState") != "Registered":
            raise ValueError(f"An administrator must register {namespace}; no auto-registration is performed.")
    with tempfile.TemporaryDirectory(prefix="aks-oidc-backend-") as directory:
        backend = Path(directory) / "backend.hcl"
        backend.write_text(backend_hcl(settings, "oidc"))
        backend.chmod(0o600)
        terraform_call(binary, ["init", "-reconfigure", "-input=false", "-lockfile=readonly",
                               f"-backend-config={backend}"], env)
        terraform_call(binary, ["fmt", "-check", "-recursive"], env)
        terraform_call(binary, ["validate", "-no-color"], env)
        if action == "plan":
            plan(bundle, binary, env, settings, inputs, identity, operation=operation)
        else:
            apply(bundle, binary, env, settings, inputs, identity, operation=operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--operation", choices=OPERATIONS, default="create")
    parser.add_argument("--bundle-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        execute(args.action, args.bundle_dir, os.environ, operation=args.operation)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, StopIteration) as exc:
        # Never dump the environment, tokens, state, or parsed plan JSON.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
