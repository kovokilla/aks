#!/usr/bin/env python3
"""Install pinned Terraform to an explicit directory without replacing existing files."""

import argparse
import hashlib
import io
from pathlib import Path
import platform
import sys
import urllib.request
import zipfile


VERSION = "1.13.3"


def install(directory, system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = machine or platform.machine()
    architecture = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in ("linux", "darwin") or architecture is None:
        raise ValueError("Only Linux/macOS amd64/arm64 are supported.")
    binary = Path(directory) / "terraform"
    if binary.exists() or binary.is_symlink():
        raise ValueError("Refusing to replace an existing Terraform executable.")
    archive = f"terraform_{VERSION}_{system}_{architecture}.zip"
    base = f"https://releases.hashicorp.com/terraform/{VERSION}/"
    with urllib.request.urlopen(base + f"terraform_{VERSION}_SHA256SUMS", timeout=120) as response:
        manifest = response.read().decode()
    hashes = [line.split()[0] for line in manifest.splitlines() if line.split()[-1:] == [archive]]
    if len(hashes) != 1:
        raise ValueError("Terraform archive checksum missing or ambiguous.")
    with urllib.request.urlopen(base + archive, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != hashes[0]:
        raise ValueError("Terraform checksum mismatch; nothing installed.")
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        contents = bundle.read("terraform")
    binary.parent.mkdir(parents=True, exist_ok=True)
    with binary.open("xb") as destination:
        destination.write(contents)
    binary.chmod(0o755)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    try:
        binary = install(args.directory)
        print(f"Terraform {VERSION} installed: {binary}")
        print("Archive SHA-256 verified against the manifest fetched over HTTPS from HashiCorp.")
        return 0
    except (ValueError, OSError, zipfile.BadZipFile, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
