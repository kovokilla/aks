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
    for check in document.get("value", []):
        resource = check.get("resource", {})
        if (resource.get("type", "").lower() != "environment"
                or str(resource.get("id")) != environment_id
                or check.get("isDisabled", False)
                or (check.get("issue") or {}).get("type") == "error"):
            continue
        enabled.add(re.sub("[^a-z]", "", check.get("type", {}).get("name", "").lower()))
    if not {"approval", "exclusivelock"}.issubset(enabled):
        raise ValueError("The environment needs enabled Approval and Exclusive lock checks.")


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
