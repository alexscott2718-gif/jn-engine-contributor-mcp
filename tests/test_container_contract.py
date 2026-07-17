"""Static guardrails for the application, image, and Compose boundary."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_is_nonroot_and_has_no_git_install():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith(
        "FROM python:3.11-slim@sha256:"
        "e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3\n"
    )
    assert "apt-get install -y --no-install-recommends ripgrep" in dockerfile
    assert " git" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'CMD ["python", "-m", "app.run"]' in dockerfile


def test_compose_keeps_content_readonly_and_secrets_gateway_only():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in compose
    gateway, cloudflared = compose.split("\n  cloudflared:\n", maxsplit=1)
    assert "API_PORT: 8788" in gateway
    assert '"127.0.0.1:${PUBLISHED_API_PORT:-8788}:8788"' in gateway
    assert "target: /data/jn-engine\n        read_only: true" in gateway
    assert "target: /secrets" in gateway
    assert "target: /audit" in gateway
    assert "read_only: true" in gateway
    assert "no-new-privileges:true" in gateway
    assert "cap_drop:\n      - ALL" in gateway
    assert "/data/jn-engine" not in cloudflared
    assert "/secrets" not in cloudflared
    assert "/audit" not in cloudflared
    assert "${CLOUDFLARED_IMAGE:?set an image pinned by sha256 digest}" in cloudflared


def test_gateway_repository_compose_is_a_separate_hardened_service():
    compose = (ROOT / "docker-compose.gateway-repo.yml").read_text(encoding="utf-8")
    assert "gateway-repo:" in compose
    assert "SERVICE_PROFILE: gateway_repository" in compose
    assert "target: /data/jn-engine-contributor-mcp\n        read_only: true" in compose
    assert "target: /secrets" in compose
    assert "target: /audit" not in compose
    assert "/data/jn-engine\n" not in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "/var/run/docker.sock" not in compose


def test_docker_context_is_allowlisted():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ignore[0] == "*"
    assert "!.env" not in ignore
    assert "!app/**" in ignore


def test_subprocess_is_confined_to_the_literal_search_engine():
    subprocess_files: list[str] = []
    dangerous_calls: list[tuple[str, str]] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if "subprocess" in names:
                    subprocess_files.append(relative)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                    dangerous_calls.append((relative, node.func.id))
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr in {"system", "popen"}
                ):
                    dangerous_calls.append((relative, f"os.{node.func.attr}"))
    assert subprocess_files == ["app/core/content_search.py"]
    assert dangerous_calls == []


def test_core_modules_have_no_http_client_and_api_token_does_not_exist():
    forbidden_imports = {"httpx", "requests", "urllib.request", "aiohttp"}
    found: list[tuple[str, str]] = []
    for path in sorted((ROOT / "app/core").glob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_imports:
                        found.append((relative, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in forbidden_imports:
                    found.append((relative, module))
    assert found == []
    application_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "app").rglob("*.py"))
    )
    assert "API_TOKEN" not in application_text
    assert "shell=True" not in application_text
