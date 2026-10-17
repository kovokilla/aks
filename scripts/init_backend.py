#!/usr/bin/env python3
"""Initialize the existing Azure backend using the current human Azure CLI login."""

import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

from bootstrap_state import Azure, DEFAULT_CONFIG, backend_hcl, load_config


ROOT = Path(__file__).resolve().parents[1]
TERRAFORM_VERSION = "1.13.3"


def initialize():
    config = load_config(DEFAULT_CONFIG)
    terraform = shutil.which("terraform")
    if not terraform or not shutil.which("az"):
        raise ValueError("Terraform 1.13.3 and an authenticated Azure CLI are required.")
    version = json.loads(subprocess.check_output(
        [terraform, "version", "-json"], text=True,
    ))["terraform_version"]
    if version != TERRAFORM_VERSION:
        raise ValueError(f"Use Terraform {TERRAFORM_VERSION}; found {version}. See README.")
    account = Azure(config["subscription_id"]).call("account", "show")
    if (account["id"].lower() != config["subscription_id"]
            or account["tenantId"].lower() != config["tenant_id"]
            or account.get("state") != "Enabled"):
        raise ValueError("Azure CLI subscription/tenant mismatch or disabled subscription.")
    env = os.environ.copy()
    # Select the human CLI session, not unrelated inherited SP/OIDC credentials.
    for name in list(env):
        if name.startswith("ARM_") or name.startswith("TF_CLI_ARGS"):
            env.pop(name)
    env.update({
        "ARM_USE_AZUREAD": "true", "ARM_USE_CLI": "true",
        "ARM_USE_OIDC": "false", "ARM_USE_MSI": "false",
        "ARM_SUBSCRIPTION_ID": config["subscription_id"],
        "ARM_TENANT_ID": config["tenant_id"],
        "TF_INPUT": "0",
    })
    # Only identifiers/settings are written, never credentials. Auto-removed afterward.
    with tempfile.TemporaryDirectory(prefix="aks-backend-") as directory:
        backend = Path(directory) / "backend.hcl"
        backend.write_text(backend_hcl(config, "cli"))
        backend.chmod(0o600)
        command = [
            terraform, f"-chdir={ROOT / 'terraform'}", "init", "-input=false",
            "-reconfigure", "-lockfile=readonly", "-no-color",
            f"-backend-config={backend}",
        ]
        result = subprocess.run(command, env=env, check=False)
    if result.returncode:
        return result.returncode
    print("Backend initialization succeeded using the current Azure CLI identity.")
    print("This does not verify the pipeline identity, locking, or AKS deployment permissions.")
    print("No terraform apply or destroy was run.")
    return 0


def main():
    try:
        return initialize()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
