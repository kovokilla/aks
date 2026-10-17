#!/usr/bin/env python3
"""Bootstrap separate Azure state storage; default mode is offline preview."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "backend.json"
BLOB_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
OWNER_TAG = "aks-terraform-bootstrap"
AZURE_ERROR_HINTS = {
    "AuthorizationFailed": "The signed-in identity lacks the required Azure RBAC action.",
    "RequestDisallowedByAzure": "Azure blocked the request; inspect MFA and policy requirements.",
    "PrincipalNotFound": "Verify the Enterprise application Object ID and tenant; allow propagation.",
    "PrincipalTypeNotSupported": "Use the Enterprise application Object ID, not the app registration Object ID.",
    "RoleAssignmentExists": "A matching assignment already exists; rerun to discover it.",
    "RoleAssignmentUpdateNotPermitted": "The assignment ID conflicts with an existing assignment.",
    "RoleDefinitionDoesNotExist": "Verify the role definition is available in this subscription.",
    "InvalidScope": "Verify the target container resource ID and its existence.",
}
FIELDS = {
    "subscription_id", "tenant_id", "location", "environment",
    "resource_group_name", "storage_account_name", "container_name", "key",
}


def validate_uuid(value):
    try:
        parsed = uuid.UUID(value)
        if parsed.int == 0:
            raise ValueError("Nil UUID is not allowed.")
        return str(parsed)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Expected a nonzero UUID, not a name or placeholder.") from exc


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError("Configuration must contain exactly: " + ", ".join(sorted(FIELDS)))
    if not all(isinstance(value, str) and value for value in config.values()):
        raise ValueError("All configuration values must be nonempty strings.")
    for field in ("subscription_id", "tenant_id"):
        config[field] = validate_uuid(config[field])
    patterns = {
        "location": r"[a-z0-9]+",
        "environment": r"[a-z][a-z0-9-]{1,39}",
        "resource_group_name": r"[a-zA-Z0-9_-]{1,90}",
        "storage_account_name": r"[a-z0-9]{3,24}",
        "container_name": r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",
        "key": r"[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,255}",
    }
    for field, pattern in patterns.items():
        if not re.fullmatch(pattern, config[field]):
            raise ValueError("Invalid configuration field: " + field)
    if "--" in config["container_name"]:
        raise ValueError("Container names cannot contain consecutive hyphens.")
    if any(part in ("", ".", "..") for part in config["key"].split("/")):
        raise ValueError("State key must be a relative blob path without traversal.")
    return config


def backend_hcl(config):
    """Noncredential settings only; native OIDC identity is supplied at runtime."""
    fields = ("resource_group_name", "storage_account_name", "container_name",
              "key", "subscription_id", "tenant_id")
    lines = [f"{field} = {json.dumps(config[field])}" for field in fields]
    return "\n".join(lines + ["use_azuread_auth = true", "use_oidc = true"]) + "\n"


class Azure:
    def __init__(self, subscription):
        self.subscription = subscription

    def call(self, *args):
        command = ["az", *args, "--subscription", self.subscription,
                   "--only-show-errors", "--output", "json"]
        # Never request keys/tokens or dump CLI responses.
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            # Only known codes/hints are emitted; raw responses may contain private data.
            details = " ".join(
                f"{code}: {hint}" for code, hint in AZURE_ERROR_HINTS.items()
                if re.search(r"\b" + re.escape(code) + r"\b", result.stderr or "")
            )
            raise RuntimeError(
                "Azure CLI failed: " + " ".join(args[:3])
                + (". " + details if details else "")
                + ". Check Azure activity logs, MFA, permissions, policy, or RBAC "
                "propagation before retrying. No rollback was attempted."
            )
        return json.loads(result.stdout) if result.stdout.strip() else None


def require_owned(resource, config, label):
    tags = resource.get("tags") or {}
    if tags.get("managed-by") != OWNER_TAG or tags.get("environment") != config["environment"]:
        raise ValueError(f"Refusing to adopt unrelated {label}; ownership tags do not match.")
    if resource.get("location") != config["location"]:
        raise ValueError(f"Existing {label} is in a different location.")


def bootstrap(config, azure, principal):
    principal = validate_uuid(principal)
    account = azure.call("account", "show")
    if (account["id"].lower() != config["subscription_id"]
            or account["tenantId"].lower() != config["tenant_id"]
            or account.get("state") != "Enabled"):
        raise ValueError("Target subscription/tenant does not match or subscription is disabled.")

    rg = config["resource_group_name"]
    name = config["storage_account_name"]
    container = config["container_name"]
    tags = [f"managed-by={OWNER_TAG}", f"environment={config['environment']}"]
    group_exists = azure.call("group", "exists", "--name", rg)
    if group_exists:
        require_owned(azure.call("group", "show", "--name", rg), config, "resource group")
    accounts = azure.call("storage", "account", "list", "--resource-group", rg) if group_exists else []
    existing = next((item for item in accounts if item["name"] == name), None)
    existing_container = None
    # Complete ownership/name preflight before any Azure mutation.
    if existing:
        require_owned(existing, config, "storage account")
        if existing.get("kind") != "StorageV2" or existing.get("sku", {}).get("name") != "Standard_LRS":
            raise ValueError("Existing state account must be StorageV2 / Standard_LRS.")
        if existing.get("isHnsEnabled"):
            raise ValueError("Hierarchical namespace is incompatible with blob versioning.")
        containers = azure.call("storage", "container-rm", "list",
                                "--storage-account", name, "--resource-group", rg)
        existing_container = next((item for item in containers if item["name"] == container), None)
        if existing_container:
            # List responses can omit metadata; validate the full resource instead.
            existing_container = azure.call(
                "storage", "container-rm", "show", "--storage-account", name,
                "--resource-group", rg, "--name", container,
            )
            metadata = existing_container.get("metadata") or {}
            if (metadata.get("managed_by") != OWNER_TAG
                    or metadata.get("environment") != config["environment"]):
                raise ValueError("Refusing to adopt an unrelated existing container.")
    elif not azure.call("storage", "account", "check-name", "--name", name)["nameAvailable"]:
        raise ValueError("Storage account name is unavailable; choose another in the configuration.")

    if not group_exists:
        azure.call("group", "create", "--name", rg, "--location", config["location"],
                   "--tags", *tags)
    security = ["--https-only", "true", "--min-tls-version", "TLS1_2",
                "--allow-blob-public-access", "false", "--allow-shared-key-access", "false"]
    if not existing:
        azure.call("storage", "account", "create", "--name", name, "--resource-group", rg,
                   "--location", config["location"], "--sku", "Standard_LRS",
                   "--kind", "StorageV2", "--public-network-access", "Enabled",
                   "--tags", *tags, *security)
    else:
        azure.call("storage", "account", "update", "--name", name, "--resource-group", rg,
                   *security)
    azure.call("storage", "account", "blob-service-properties", "update",
               "--account-name", name, "--resource-group", rg,
               "--enable-versioning", "true",
               "--enable-delete-retention", "true", "--delete-retention-days", "14",
               "--enable-container-delete-retention", "true",
               "--container-delete-retention-days", "14")
    if not existing_container:
        # Management-plane API avoids account keys and bootstrap blob-data access.
        azure.call("storage", "container-rm", "create", "--storage-account", name,
                   "--resource-group", rg, "--name", container, "--public-access", "off",
                   "--fail-on-exist", "--metadata", f"managed_by={OWNER_TAG}",
                   f"environment={config['environment']}")
    else:
        azure.call("storage", "container-rm", "update", "--storage-account", name,
                   "--resource-group", rg, "--name", container, "--public-access", "off")

    scope = (f"/subscriptions/{config['subscription_id']}/resourceGroups/{rg}"
             f"/providers/Microsoft.Storage/storageAccounts/{name}"
             f"/blobServices/default/containers/{container}")
    assignments = azure.call("role", "assignment", "list", "--scope", scope,
                             "--fill-principal-name", "false")
    assigned = any(
        item.get("principalId", "").lower() == principal
        and item.get("roleDefinitionId", "").lower().endswith("/" + BLOB_CONTRIBUTOR)
        and item.get("scope", "").lower() == scope.lower()
        for item in assignments
    )
    if not assigned:
        role_name = str(uuid.uuid5(uuid.NAMESPACE_URL, scope.lower() + principal + BLOB_CONTRIBUTOR))
        azure.call("role", "assignment", "create", "--name", role_name,
                   "--assignee-object-id", principal, "--assignee-principal-type", "ServicePrincipal",
                   "--role", BLOB_CONTRIBUTOR, "--scope", scope)
    return scope


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Create/harden Azure state storage.")
    mode.add_argument("--print-backend-config", action="store_true",
                      help="Print backend HCL for native OIDC; no Azure calls.")
    parser.add_argument("--confirm-subscription", help="Must exactly match config before --apply.")
    parser.add_argument("--pipeline-principal-object-id",
                        help="Enterprise application's service principal Object ID, NOT client ID.")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.print_backend_config:
            print(backend_hcl(config), end="")
            return 0
        if not args.apply:
            print("PREVIEW ONLY: no Azure requests or changes; availability/permissions not checked.")
            print(json.dumps(config, indent=2))
            print("Will create/harden tagged state storage and grant the specified pipeline identity")
            print("Storage Blob Data Contributor at container scope. No AKS or ACR changes.")
            return 0
        if args.confirm_subscription != config["subscription_id"]:
            raise ValueError("--confirm-subscription must exactly match the configured subscription ID.")
        principal = validate_uuid(args.pipeline_principal_object_id)
        if not shutil.which("az"):
            raise ValueError("Azure CLI is required for --apply; use Azure Cloud Shell or install it.")
        scope = bootstrap(config, Azure(config["subscription_id"]), principal)
        print("State storage configured. Container scope: " + scope)
        print("Terraform creates the state blob on its first state write. Allow time for RBAC propagation.")
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
