#!/usr/bin/env python3
"""Verify private project and sandbox context; native Azure DevOps checks enforce gates."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from bootstrap_state import DEFAULT_CONFIG, load_config, validate_uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirects are not allowed for authenticated checks requests.")


def project_url(env):
    collection = urllib.parse.urlsplit(env.get("SYSTEM_COLLECTIONURI", ""))
    if (collection.scheme != "https" or collection.netloc != "dev.azure.com"
            or collection.query or collection.fragment):
        raise ValueError("Expected the official https://dev.azure.com collection URI.")
    project = validate_uuid(env.get("SYSTEM_TEAMPROJECTID"))
    environment_id = env.get("ENVIRONMENT_ID", "")
    if not environment_id.isdigit() or int(environment_id) < 1:
        raise ValueError("The deployment job must provide a positive numeric Environment.Id.")
    return (urllib.parse.urlunsplit(collection).rstrip("/")
            + f"/_apis/projects/{project}?api-version=7.1")


def read_json(url, token):
    headers = {
        "Authorization": "Bearer " + token, "Accept": "application/json",
    }
    request = urllib.request.Request(url, headers=headers)
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=60) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, ValueError) as exc:
        # Do not print bodies, headers, URLs with query data, or OAuth tokens.
        raise RuntimeError(
            "Cannot read project metadata. Verify the job OAuth token mapping and "
            "the project Build Service's project read access."
        ) from exc


def verify(env):
    settings = load_config(DEFAULT_CONFIG)
    if env.get("ENVIRONMENT_NAME") != settings["environment"]:
        raise ValueError("The deployment environment does not match the sandbox configuration.")
    url = project_url(env)
    token = env.get("SYSTEM_ACCESSTOKEN", "")
    if not token or token.startswith("$("):
        raise ValueError("Map System.AccessToken to the task's SYSTEM_ACCESSTOKEN environment.")
    project = read_json(url, token)
    identifier = project.get("id") if isinstance(project, dict) else None
    if not isinstance(identifier, str) or identifier.lower() != env["SYSTEM_TEAMPROJECTID"].lower():
        raise ValueError("Azure DevOps project response does not match the job's project.")
    if project.get("visibility") != "private":
        raise ValueError("Use a private Azure DevOps project to protect Terraform logs and plan artifacts.")
    print(f"Verified private project and sandbox environment context (ID {env['ENVIRONMENT_ID']}).")
    print("Approval and Exclusive lock are enforced by Azure DevOps when configured on the environment.")
    print("This preflight does not audit check presence or change environment permissions/checks.")


def main():
    try:
        verify(os.environ)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
