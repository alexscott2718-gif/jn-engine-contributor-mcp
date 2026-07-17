"""Credential-free contributor access-kit contract."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = {
    "README.md",
    "SHA256SUMS",
    "api_usage.md",
    "endpoint.json",
    "mcp_surface.md",
    "onboarding_contributor.md",
    "security_model.md",
}


def test_access_kit_is_complete_and_contains_no_secret_material(tmp_path: Path):
    output = tmp_path / "access-kit.tar.gz"
    result = subprocess.run(
        [str(REPOSITORY_ROOT / "deploy" / "make_access_kit.sh"), str(output)],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert "secret scan: clean" in result.stdout
    with tarfile.open(output, "r:gz") as archive:
        members = {
            member.name: member
            for member in archive.getmembers()
            if member.isfile()
        }
        prefix = "jn-engine-contributor-mcp-access-kit/"
        relative_names = {name.removeprefix(prefix) for name in members}
        assert relative_names == REQUIRED_FILES
        assert all(name.startswith(prefix) for name in members)

        payloads = {
            name.removeprefix(prefix): archive.extractfile(member).read()
            for name, member in members.items()
        }

    endpoint = json.loads(payloads["endpoint.json"])
    assert endpoint["mcp_url"] == "https://mcp.example.org/mcp"
    assert endpoint["repository"] == "alexscott2718-gif/jn-engine"

    checksums = payloads["SHA256SUMS"].decode("ascii").splitlines()
    for line in checksums:
        expected, filename = line.split("  ", 1)
        assert hashlib.sha256(payloads[filename]).hexdigest() == expected

    combined = b"\n".join(payloads.values())
    assert b"BEGIN PRIVATE KEY" not in combined
    assert b"github_pat_" not in combined
    assert b"/secrets/" not in combined
