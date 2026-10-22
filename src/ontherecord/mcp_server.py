"""The assistant's tools over MCP, for Claude Desktop, Claude Code or any MCP client: `ontherecord mcp` (stdio).

The tools are the ones in tools.py. MCP clients ask the person before a tool runs; the tools that start or stop
work also say so in their annotations. Nothing here deletes anything: clearing a camera card stays a button in the
GUI.

Claude Code:     claude mcp add ontherecord -- ontherecord mcp
Claude Desktop:  {"mcpServers": {"ontherecord": {"command": "ontherecord", "args": ["mcp"]}}}
                 (use the full path to the `ontherecord` in the install's environment if it is not on PATH)
"""
from __future__ import annotations

from .config import MachineConfig
from .tools import Toolbox

INSTRUCTIONS = ("On the Record makes transcripts of recorded meetings and finds what people said in them. Look "
                "things up with find_recordings, search_transcripts, recording_overview and read_transcript; start "
                "work with transcribe or import_device; follow it with jobs_status. Quotes come with a time and a "
                "#play link that opens the recording at that moment in the GUI (http://127.0.0.1:8765/).")


def build(cfg: MachineConfig):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
    tb = Toolbox(cfg)
    server = MCPServer(name="ontherecord", title="On the Record", instructions=INSTRUCTIONS)
    for t in tb.tools():
        server.add_tool(t.fn, name=t.name, description=t.description,
                        annotations=ToolAnnotations(read_only_hint=not t.confirm, destructive_hint=False,
                                                    open_world_hint=t.name == "transcribe"))
    return server


def serve_mcp(cfg: MachineConfig) -> None:
    build(cfg).run("stdio")
