"""Loopback-only contributor development mode."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


REAL_SNAPSHOT = Path(os.environ.get("JN_TEST_SNAPSHOT_PATH", "/srv/jn-mcp/test-snapshot"))


def test_authless_local_serves_snapshot_without_runtime_secrets():
    settings = Settings(
        app_env="development",
        api_host="127.0.0.1",
        auth_mode="authless_local",
        jn_snapshot_path=REAL_SNAPSHOT,
        search_engine="python",
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        search = client.get(
            "/v1/content/search",
            params={"q": "C3DPlayer", "scope": "re", "limit": 1},
        )
        assert search.status_code == 200
        assert search.json()["results"]
