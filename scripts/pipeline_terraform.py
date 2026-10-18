#!/usr/bin/env python3
"""Azure DevOps OIDC plan/apply runner; never performs destroy or bootstrap."""

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
    return isinstance(actual, str) and actual.rstrip("/").lower() == expected.lower()


def validate_plan(document, settings, inputs):
    """Fail closed on state outside the sandbox, deletes, imports, and replacement."""
    if document.get("terraform_version") != VERSION or document.get("errored", False):
        raise ValueError("Plan version mismatch or errored Terraform plan.")
    planned_inputs = {key: value["value"] for key, value in document.get("variables", {}).items()}
    if planned_inputs != inputs:
        raise ValueError("Plan variables differ from the committed sandbox inputs.")
    changes = document.get("resource_changes", [])
    if {item["address"] for item in changes} != ADDRESSES or len(changes) != len(ADDRESSES):
        raise ValueError("Plan must contain only the four declared sandbox resources.")
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


def terraform_call(binary, args, env, json_output=False, accepted=(0,)):
    command = [binary, f"-chdir={ROOT / 'terraform'}", *args]
    result = subprocess.run(command, env=env, check=False, text=True,
                            capture_output=json_output)
    if result.returncode not in accepted:
        raise RuntimeError(f"Terraform {args[0]} failed (exit {result.returncode}).")
    # show -json can contain secrets: retain it in memory, never log or publish it.
    return json.loads(result.stdout) if json_output else result.returncode


def plan(bundle, binary, env, settings, inputs, identity):
    bundle.mkdir(parents=True, exist_ok=False, mode=0o700)
    plan_path = bundle / PLAN_NAME
    terraform_call(binary, ["plan", "-input=false", "-lock-timeout=5m",
                           "-detailed-exitcode", f"-out={plan_path}"], env, accepted=(0, 2))
    plan_path.chmod(0o600)
    document = terraform_call(binary, ["show", "-json", str(plan_path)], env, json_output=True)
    validate_plan(document, settings, inputs)
    manifest = dict(identity, source_sha256=source_digest(),
                    plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest())
    path = bundle / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    path.chmod(0o600)
    print("Sandbox-only plan validated; review the Terraform plan log before approving.")


def apply(bundle, binary, env, settings, inputs, identity):
    if {p.name for p in bundle.iterdir()} != {PLAN_NAME, MANIFEST_NAME}:
        raise ValueError("Plan artifact must contain exactly its binary plan and manifest.")
    if any(p.is_symlink() or not p.is_file() for p in bundle.iterdir()):
        raise ValueError("Plan artifact must contain regular files, not links.")
    plan_path = bundle / PLAN_NAME
    manifest = json.loads((bundle / MANIFEST_NAME).read_text())
    expected = dict(identity, source_sha256=source_digest(),
                    plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest())
    if manifest != expected:
        raise ValueError("Plan hash, source, run, commit, version, or pipeline identity mismatch.")
    document = terraform_call(binary, ["show", "-json", str(plan_path)], env, json_output=True)
    validate_plan(document, settings, inputs)
    terraform_call(binary, ["apply", "-input=false", "-lock-timeout=5m", str(plan_path)], env)


def execute(action, bundle, source):
    settings = load_config(DEFAULT_CONFIG)
    inputs = json.loads((ROOT / "terraform" / "sandbox.auto.tfvars.json").read_text())
    provisioning = source.get("PROVISION_REQUESTED", "false").lower() == "true"
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
            plan(bundle, binary, env, settings, inputs, identity)
        else:
            apply(bundle, binary, env, settings, inputs, identity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--bundle-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        execute(args.action, args.bundle_dir, os.environ)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, StopIteration) as exc:
        # Never dump the environment, tokens, state, or parsed plan JSON.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
