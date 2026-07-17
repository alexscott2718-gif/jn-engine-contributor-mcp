"""Fail when tracked public files contain common identity or infrastructure leaks."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+[.])+[A-Za-z]{2,}"
)
PRIVATE_IPV4 = re.compile(
    r"(?<![0-9])(?:10[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}"
    r"|172[.](?:1[6-9]|2[0-9]|3[01])[.][0-9]{1,3}[.][0-9]{1,3}"
    r"|192[.]168[.][0-9]{1,3}[.][0-9]{1,3})(?![0-9])"
)
USER_PATHS = (
    re.compile(re.escape("/" + "home" + "/") + r"[^/\s]+/"),
    re.compile(re.escape("/" + "Users" + "/") + r"[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\" + "Users" + r"\\[^\\\s]+\\", re.IGNORECASE),
)
ALLOWED_EMAIL_DOMAINS = (
    ".example",
    ".example.com",
    ".invalid",
    ".test",
    "users.noreply.github.com",
)
IGNORED_DIRECTORIES = {".git", ".venv", "build", "dist", "__pycache__"}
FORBIDDEN_PATH_NAMES = {
    ".env",
    ".env.gateway-repo",
    "credentials.json",
    "tool_calls.ndjson",
}


def _tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return [
            root / value.decode("utf-8")
            for value in result.stdout.split(b"\0")
            if value
        ]
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts)
    )


def _allowed_email(value: str) -> bool:
    domain = value.rsplit("@", 1)[1].lower()
    return domain in ALLOWED_EMAIL_DOMAINS or any(
        domain.endswith(suffix) for suffix in ALLOWED_EMAIL_DOMAINS
    )


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in _tracked_files(root):
        relative = path.relative_to(root)
        if path.name in FORBIDDEN_PATH_NAMES:
            findings.append(f"{relative}: forbidden runtime-data filename")
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            findings.append(f"{relative}: cannot read: {exc}")
            continue
        if b"\0" in raw[:4096]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(f"{relative}: non-UTF-8 tracked file requires review")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in EMAIL.finditer(line):
                if not _allowed_email(match.group(0)):
                    findings.append(
                        f"{relative}:{line_number}: public email address"
                    )
            if PRIVATE_IPV4.search(line):
                findings.append(f"{relative}:{line_number}: private IPv4 address")
            if any(pattern.search(line) for pattern in USER_PATHS):
                findings.append(f"{relative}:{line_number}: absolute user-home path")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan(root)
    if findings:
        print("public-tree privacy check failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("public-tree privacy check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
