#!/usr/bin/env python3
"""Read Azure DevOps checks; never create or modify checks or Azure resources."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from bootstrap_state import DEFAULT_CONFIG, load_config, validate_uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirects are not allowed for authenticated checks requests.")


def checks_url(env):
    collection = urllib.parse.urlsplit(env.get("SYSTEM_COLLECTIONURI", ""))
    if (collection.scheme != "https" or collection.netloc != "dev.azure.com"
            or collection.query or collection.fragment):
        raise ValueError("Expected the official https://dev.azure.com collection URI.")
    project = validate_uuid(env.get("SYSTEM_TEAMPROJECTID"))
    environment_id = env.get("ENVIRONMENT_ID", "")
    if not environment_id.isdigit():
        raise ValueError("The deployment job must provide a numeric Environment.Id.")
    query = urllib.parse.urlencode({
        "resourceType": "environment", "resourceId": environment_id,
        "api-version": "7.1-preview.1",
    })
    return (urllib.parse.urlunsplit(collection).rstrip("/")
            + f"/{project}/_apis/pipelines/checks/configurations?{query}")


def require_checks(document, environment_id):
    enabled = set()
    observed = []
    for check in document.get("value", []):
        resource = check.get("resource") or {}
        check_type = check.get("type") or {}
        kind = re.sub("[^a-z]", "", check_type.get("name", "").lower())
        # UI/API versions can use either Approval or Approvals.
        if kind == "approvals" or check_type.get("id", "").lower() == "8c6f20a7-a545-4486-9777-f762fafe0d4d":
            kind = "approval"
        matches = (resource.get("type", "").lower() == "environment"
                   and str(resource.get("id")) == environment_id)
        disabled = bool(check.get("isDisabled", False))
        issue = str((check.get("issue") or {}).get("type", "")).lower() == "error"
        # Only type labels/flags are reported, never complete API responses.
        observed.append(f"{kind[:48] or 'unknown'}(scopeMatch={matches},disabled={disabled},issue={issue})")
        if not matches or disabled or issue:
            continue
        enabled.add(kind)
    required = {"approval": "Approval", "exclusivelock": "Exclusive lock"}
    missing = [label for kind, label in required.items() if kind not in enabled]
    if missing:
        raise ValueError(
            f"Environment {environment_id} is missing enabled checks: {', '.join(missing)}. "
            f"Observed API check types: {'; '.join(observed[:20]) or 'none'}. "
            "Start a new run after saving environment checks; do not retry only the failed job."
        )


def read_json(url, token):
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json",
    })
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=60) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, ValueError) as exc:
        # Do not print bodies, headers, URLs with query data, or OAuth tokens.
        raise RuntimeError(
            "Cannot read project/environment checks. Grant the project Build Service read access "
            "to this environment/check configuration and verify the OAuth token mapping."
        ) from exc


def verify(env):
    settings = load_config(DEFAULT_CONFIG)
    if env.get("ENVIRONMENT_NAME") != settings["environment"]:
        raise ValueError("The deployment environment does not match the sandbox configuration.")
    url = checks_url(env)
    token = env.get("SYSTEM_ACCESSTOKEN", "")
    if not token or token.startswith("$("):
        raise ValueError("Map System.AccessToken to the task's SYSTEM_ACCESSTOKEN environment.")
    project_url = (env["SYSTEM_COLLECTIONURI"].rstrip("/") + "/_apis/projects/"
                   + env["SYSTEM_TEAMPROJECTID"] + "?api-version=7.1")
    project = read_json(project_url, token)
    if project.get("visibility") != "private":
        raise ValueError("Use a private Azure DevOps project to protect Terraform logs and plan artifacts.")
    document = read_json(url, token)
    require_checks(document, env["ENVIRONMENT_ID"])
    print("Verified enabled Approval and Exclusive lock checks on the sandbox environment.")


def main():
    try:
        verify(os.environ)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
