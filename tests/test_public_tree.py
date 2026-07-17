"""Public-tree privacy guard acceptance tests."""

from pathlib import Path

from scripts.check_public_tree import scan


def test_public_tree_guard_accepts_documented_examples(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "https://mcp.example.org user@example.test /srv/project 127.0.0.1\n",
        encoding="utf-8",
    )
    assert scan(tmp_path) == []


def test_public_tree_guard_rejects_identity_and_private_network_data(tmp_path: Path):
    (tmp_path / "notes.txt").write_text(
        "person" + "@" + "personal-domain.org\n"
        + "/" + "home" + "/operator/project\n"
        + "192" + ".168.50.2\n",
        encoding="utf-8",
    )
    findings = scan(tmp_path)
    assert any("public email address" in finding for finding in findings)
    assert any("absolute user-home path" in finding for finding in findings)
    assert any("private IPv4 address" in finding for finding in findings)


def test_public_tree_guard_rejects_runtime_data_names(tmp_path: Path):
    (tmp_path / ("." + "env")).write_text("TOKEN=value\n", encoding="utf-8")
    assert scan(tmp_path) == [".env: forbidden runtime-data filename"]
