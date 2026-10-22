"""The MCP server, spoken to by the MCP SDK's own client over stdio, as Claude Desktop or Claude Code would."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")


def test_mcp_lists_and_calls_tools(tmp_path):
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "ontherecord.cli", "--config", "none",
                                                                 "--workspace", str(tmp_path / "ws"), "mcp"])

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                assert {"find_recordings", "search_transcripts", "transcribe", "cancel_job"} <= set(tools)
                assert tools["find_recordings"].annotations.read_only_hint is True
                assert tools["transcribe"].annotations.read_only_hint is False
                assert "links" in tools["transcribe"].input_schema["properties"]
                r = await session.call_tool("find_recordings", {"words": ""})
                return r.content[0].text

    text = anyio.run(go)
    assert "library is empty" in text
