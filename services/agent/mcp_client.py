"""MCP client helper for the image-processing MCP server.

The agent talks to the image-processing tools (rotate, flip, blur, resize,
crop, add_noise) through the Model Context Protocol over a stdio transport.
For local development we spawn the MCP server as a subprocess:

    IMG_PROC_MCP_COMMAND   Executable that runs the server (default: this
                           interpreter, i.e. sys.executable).
    IMG_PROC_MCP_ARGS      Space-separated args passed to the command. When
                           unset, defaults to the path of
                           ../img-proc-mcp/app.py relative to this file.

The MCP Python SDK is async-only, so `call_mcp_tool` wraps a short-lived async
session in `asyncio.run` to expose a simple synchronous call to the agent's
ReAct loop. A fresh subprocess/session is used per call to keep the flow
explicit and easy to follow.
"""

import asyncio
import os
import shlex
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _server_params() -> StdioServerParameters:
    """Build the stdio parameters used to launch the img-proc MCP server."""
    command = os.environ.get("IMG_PROC_MCP_COMMAND") or sys.executable

    args_env = os.environ.get("IMG_PROC_MCP_ARGS")
    if args_env:
        args = shlex.split(args_env)
    else:
        default_app = os.path.join(
            os.path.dirname(__file__), "..", "img-proc-mcp", "app.py"
        )
        args = [os.path.abspath(default_app)]

    return StdioServerParameters(command=command, args=args)


def _extract_text(result) -> str:
    """Concatenate the text blocks of an MCP CallToolResult into one string."""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts).strip()


async def _acall_tool(name: str, arguments: dict) -> str:
    """Open a stdio MCP session, call `name` with `arguments`, return its text."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)

    if result.isError:
        raise RuntimeError(_extract_text(result) or "MCP tool call failed")
    return _extract_text(result)


def call_mcp_tool(name: str, arguments: dict) -> str:
    """Synchronously call the MCP tool `name` with `arguments` and return its
    text result (for the image tools this is the processed image's S3 key)."""
    return asyncio.run(_acall_tool(name, arguments))
