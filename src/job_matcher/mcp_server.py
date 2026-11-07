"""MCP server over the offers: the same tools as the agent, for any MCP client (Claude Code, Claude
Desktop, Cursor). Runs over stdio for a local client; api.py also serves it over streamable HTTP at /mcp.

    claude mcp add jobs -- uv run --directory /path/to/job-matcher jm mcp
"""
from __future__ import annotations

import inspect

from mcp.server.mcpserver import MCPServer

from job_matcher.tools import Toolbox, toolbox_from_env

INSTRUCTIONS = (
    "Open job offers in Poland, mostly tech, collected from job boards and company career pages. "
    "search_offers takes free text or a whole CV and applies hard filters (level, years, city, role family) "
    "before returning the nearest offers. Level and years come from LLM extraction with quotes from the ad; "
    "get_offer shows them. Link to offers by their url."
)


def build(toolbox: Toolbox) -> MCPServer:
    server = MCPServer("job-matcher", instructions=INSTRUCTIONS)
    for name, fn in toolbox.functions().items():
        server.add_tool(fn, name=name, description=inspect.getdoc(fn))
    return server


def main() -> None:
    build(toolbox_from_env()).run("stdio")


if __name__ == "__main__":
    main()
