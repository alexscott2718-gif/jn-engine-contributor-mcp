"""Exact six-tool MCP contracts over the real immutable snapshot."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import jsonref
import pytest
from fastmcp.exceptions import ToolError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from uvicorn import Config, Server

from app.config import Settings
from app.collaboration.errors import bad_args
from app.core.path_safety import encode_content_id
from app.main import build_data_plane
from app.mcp.server import create_mcp_server
from app.models.content import FetchOutput, SearchToolOutput
from app.models.projects import ProjectContextOutput
from app.models.symbols import SymbolLookupOutput
from app.models.status import CheckStatusOutput
from app.models.tasks import TaskListOutput

REAL_SNAPSHOT = Path(
    os.environ.get(
        "JN_TEST_SNAPSHOT_PATH",
        "/srv/jn-engine-contributor-mcp/test-snapshots/"
        "925242073a771aa68996c294aec8cc41cb43a0ef",
    )
)
EXPECTED_TOOLS = [
    "search",
    "fetch",
    "list_tasks",
    "project_context",
    "lookup_symbol",
    "check_status",
]


class _UnusedStatusService:
    error = None
    last_kwargs = None

    def check(self, **_kwargs):
        self.last_kwargs = _kwargs
        if self.error is not None:
            raise self.error
        raise AssertionError("live status service is not used by immutable-tool tests")


@pytest.fixture(scope="module")
def status_service():
    return _UnusedStatusService()


@pytest.fixture(scope="module")
def mcp_server(status_service):
    settings = Settings(
        auth_mode="authless_local",
        api_host="127.0.0.1",
        jn_snapshot_path=REAL_SNAPSHOT,
        search_engine="python",
    )
    data = build_data_plane(settings)
    return create_mcp_server(
        settings,
        auth=None,
        content=data.content,
        tasks=data.tasks,
        projects=data.projects,
        symbols=data.symbols,
        statuses=status_service,
    )


@pytest.fixture(scope="module")
def live_mcp_url(mcp_server):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    application = mcp_server.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=True,
    )
    server = Server(
        Config(
            application,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("live MCP verification server did not start")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def _tools(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def _run(tool, arguments):
    return asyncio.run(tool.run(arguments))


def test_exact_six_tools_descriptions_schemas_and_annotations(mcp_server):
    tools = _tools(mcp_server)
    assert list(tools) == EXPECTED_TOOLS
    schemas = {
        "search": SearchToolOutput.model_json_schema(),
        "fetch": FetchOutput.model_json_schema(),
        "list_tasks": TaskListOutput.model_json_schema(),
        "project_context": ProjectContextOutput.model_json_schema(),
        "lookup_symbol": SymbolLookupOutput.model_json_schema(),
        "check_status": CheckStatusOutput.model_json_schema(),
    }
    for name, tool in tools.items():
        assert tool.description
        assert tool.parameters["type"] == "object"
        expected_output_schema = jsonref.replace_refs(
            schemas[name], proxies=False
        )
        expected_output_schema.pop("$defs", None)
        assert tool.output_schema == expected_output_schema
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is (name == "check_status")


def test_no_resources_or_prompts_are_registered(mcp_server):
    assert asyncio.run(mcp_server.list_resources()) == []
    assert asyncio.run(mcp_server.list_prompts()) == []


def test_check_status_preserves_typed_sanitized_tool_error(
    mcp_server,
    status_service,
):
    status_service.error = bad_args("provide exactly one of pr or branch")
    try:
        with pytest.raises(ToolError) as raised:
            _run(_tools(mcp_server)["check_status"], {})
    finally:
        status_service.error = None
    payload = json.loads(str(raised.value))
    assert payload == {
        "code": "bad_args",
        "detail": "provide exactly one of pr or branch",
    }
    assert status_service.last_kwargs["caller_identity"] == "local:unauthenticated"


def test_search_then_fetch_has_matching_structured_and_json_content(mcp_server):
    tools = _tools(mcp_server)
    searched = _run(
        tools["search"],
        {"query": "C3DPlayer", "scope": "re", "limit": 2},
    )
    assert json.loads(searched.content[0].text) == searched.structured_content
    hits = searched.structured_content["results"]
    assert len(hits) == 2
    assert set(hits[0]) == {"id", "title", "url"}
    assert hits[0]["id"].startswith("jn1_")
    assert hits[0]["title"] == "C3DPlayer"
    assert "/blob/925242073a771aa68996c294aec8cc41cb43a0ef/" in hits[0]["url"]

    fetched = _run(tools["fetch"], {"id": hits[0]["id"]})
    assert json.loads(fetched.content[0].text) == fetched.structured_content
    record = fetched.structured_content
    assert set(record) == {"id", "title", "text", "url", "metadata"}
    assert record["id"] == hits[0]["id"]
    assert record["title"] == "C3DPlayer"
    assert record["metadata"] == {
        "path": "docs/decomp/C3DPlayer.md",
        "kind": "reverse_engineering",
        "language": "markdown",
        "repository": "alexscott2718-gif/jn-engine",
        "ref": "refs/heads/master",
        "commit": "925242073a771aa68996c294aec8cc41cb43a0ef",
        "text_chars": len(record["text"]),
        "truncated": False,
    }


def test_official_mcp_sdk_lists_six_tools_and_searches_then_fetches(live_mcp_url):
    async def scenario():
        async with streamable_http_client(live_mcp_url) as (
            read_stream,
            write_stream,
            _session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                searched = await session.call_tool(
                    "search",
                    {
                        "query": "C3DPlayer",
                        "scope": "re",
                        "limit": 1,
                    },
                )
                hit = searched.structuredContent["results"][0]
                fetched = await session.call_tool("fetch", {"id": hit["id"]})
                return listed, searched, fetched

    listed, searched, fetched = asyncio.run(scenario())
    assert [tool.name for tool in listed.tools] == EXPECTED_TOOLS
    assert searched.isError is False
    assert fetched.isError is False
    assert fetched.structuredContent["title"] == "C3DPlayer"
    assert fetched.structuredContent["metadata"]["commit"] == (
        "925242073a771aa68996c294aec8cc41cb43a0ef"
    )
    assert json.loads(fetched.content[0].text) == fetched.structuredContent


def test_stale_fetch_is_a_short_tool_error(mcp_server):
    stale_id = encode_content_id("1" * 40, "README.md")
    with pytest.raises(ToolError, match="snapshot changed; search again") as excinfo:
        _run(_tools(mcp_server)["fetch"], {"id": stale_id})
    message = str(excinfo.value)
    assert "/home/" not in message
    assert "README.md" not in message


def test_invalid_fetch_id_is_sanitized(mcp_server):
    with pytest.raises(ToolError, match="invalid content ID") as excinfo:
        _run(_tools(mcp_server)["fetch"], {"id": "../../etc/passwd"})
    assert "/etc/passwd" not in str(excinfo.value)


def test_task_tool_uses_only_committed_sources(mcp_server):
    body = _run(
        _tools(mcp_server)["list_tasks"],
        {"status": "all", "source": "linkage", "limit": 50},
    ).structured_content
    assert body["count"] == 29
    assert body["snapshot"]["commit"] == (
        "925242073a771aa68996c294aec8cc41cb43a0ef"
    )
    assert {task["source_kind"] for task in body["tasks"]} == {"linkage"}
    assert "issues" not in json.dumps(body).casefold()


def test_project_context_tool_is_bounded_and_grounded(mcp_server):
    body = _run(
        _tools(mcp_server)["project_context"],
        {"max_chars": 1_000},
    ).structured_content
    assert body["project"] == "jn-engine"
    assert len(body["context"]) <= 1_000
    assert len(body["important_files"]) == 8
    assert len(body["open_tasks"]) == 10
    assert any("Stale branch notice" in state for state in body["current_state"])


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"name": "C3DPlayer", "limit": 50}, "C3DPlayer"),
        ({"address": "00437c40"}, "UpdateGroundMoveA"),
        ({"fourcc": "3AIT", "limit": 50}, "3AIT"),
    ],
)
def test_lookup_symbol_concrete_grounding(mcp_server, arguments, expected):
    body = _run(
        _tools(mcp_server)["lookup_symbol"],
        arguments,
    ).structured_content
    assert body["count"] > 0
    assert expected in json.dumps(body)
    if "name" in arguments:
        assert any(
            linkage["status"] == "linked-blocked"
            for linkage in body["results"][0]["linkage"]
        )
