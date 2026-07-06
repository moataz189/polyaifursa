"""MCP client helper for the image-processing MCP server.

The agent talks to the image-processing tools (rotate, flip, blur, resize,
crop, add_noise) through the Model Context Protocol over an **HTTP** transport.
Instead of wrapping each tool in a local ``@tool`` function, we connect to a
running HTTP MCP server and *discover* its tools, exactly like the course
example:

    client = MultiServerMCPClient({
        "img-proc": {
            "url": IMG_PROC_MCP_URL,
            "transport": "http",
        }
    })
    mcp_tools = await client.get_tools()   # discover tools from the server

The discovered tools are real LangChain tools. The agent binds them to the LLM
alongside its local YOLO tools, so the model calls them directly (passing the
image's ``input_key``); there are no hand-written wrappers in ``app.py``.

The server URL is configurable:

    IMG_PROC_MCP_URL   Full URL of the MCP endpoint
                       (default: http://localhost:9000/mcp).

`langchain-mcp-adapters` is async-only, so `get_mcp_tools` wraps the async
discovery in `asyncio.run` to expose a simple synchronous call to the agent's
startup code.
"""

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
# Full URL of the running image-processing MCP server. Defaults to a local
# server; override with IMG_PROC_MCP_URL to point at another host/port.
IMG_PROC_MCP_URL = os.environ.get("IMG_PROC_MCP_URL", "http://localhost:9000/mcp")


def _server_config() -> dict:
    """Return the MultiServerMCPClient config for the img-proc HTTP server."""
    return {
        "img-proc": {
            "url": IMG_PROC_MCP_URL,
            "transport": "http",
        }
    }


def _build_client():
    """Build a MultiServerMCPClient for the img-proc HTTP server.

    The import is done lazily so that merely importing this module does not
    require `langchain-mcp-adapters` to be installed (e.g. in tests that mock
    the transport).
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    return MultiServerMCPClient(_server_config())


async def _discover_tools() -> list:
    """Connect to the HTTP MCP server and return its discovered tools as a list.

    langchain-mcp-adapters 0.3.0 exposes ``get_tools()`` directly on the client,
    so we build the client and await ``get_tools()`` without an async context
    manager.
    """
    client = _build_client()
    return await client.get_tools()


def get_mcp_tools() -> list:
    """Discover the image-processing MCP tools over HTTP and return them as a
    list of LangChain tools (rotate, flip, blur, resize, crop, add_noise).

    Each returned tool is bound to the server connection, so it can be invoked
    later (via ``.ainvoke``) outside of this discovery call.
    """
    return asyncio.run(_discover_tools())
