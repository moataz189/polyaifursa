"""Tests for the HTTP MCP client (mcp_client.py).

These tests verify the agent connects to the image-processing MCP server over
HTTP and *discovers* its tools via MultiServerMCPClient.get_tools(). The
transport is fully mocked (via a fake MultiServerMCPClient), so no real MCP
server is contacted and `langchain-mcp-adapters` need not be installed.
"""

import sys
import types

import mcp_client


class _FakeTool:
    """Minimal stand-in for a discovered LangChain MCP tool."""

    def __init__(self, name):
        self.name = name


class _FakeClient:
    """Fake MultiServerMCPClient exposing get_tools() (adapter 0.3.0 shape)."""

    def __init__(self, tools):
        self._tools = tools

    async def get_tools(self):
        return self._tools


def test_default_url_points_at_local_http_mcp():
    # When IMG_PROC_MCP_URL is unset the default is the local HTTP /mcp endpoint.
    assert mcp_client.IMG_PROC_MCP_URL == "http://localhost:9000/mcp"


def test_server_config_uses_url_and_http_transport():
    config = mcp_client._server_config()
    assert config == {
        "img-proc": {
            "url": mcp_client.IMG_PROC_MCP_URL,
            "transport": "http",
        }
    }


def test_build_client_uses_url_and_http_transport(monkeypatch):
    # Inject a fake langchain_mcp_adapters.client module so the lazy import in
    # _build_client picks it up, and capture the config passed to the client.
    captured = {}

    class FakeMSC:
        def __init__(self, config):
            captured["config"] = config

    fake_client_mod = types.ModuleType("langchain_mcp_adapters.client")
    fake_client_mod.MultiServerMCPClient = FakeMSC
    fake_pkg = types.ModuleType("langchain_mcp_adapters")

    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", fake_pkg)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", fake_client_mod)

    client = mcp_client._build_client()

    assert isinstance(client, FakeMSC)
    assert captured["config"]["img-proc"]["url"] == mcp_client.IMG_PROC_MCP_URL
    assert captured["config"]["img-proc"]["transport"] == "http"


def test_get_mcp_tools_discovers_image_processing_tools(monkeypatch):
    # get_mcp_tools() should connect through MultiServerMCPClient and return the
    # discovered image-processing tools (rotate, flip, blur, resize, crop,
    # add_noise) as a list of LangChain tools.
    seen_config = {}
    discovered = [
        _FakeTool("rotate"),
        _FakeTool("flip"),
        _FakeTool("blur"),
        _FakeTool("resize"),
        _FakeTool("crop"),
        _FakeTool("add_noise"),
    ]

    def fake_build_client():
        seen_config["config"] = mcp_client._server_config()
        return _FakeClient(discovered)

    monkeypatch.setattr(mcp_client, "_build_client", fake_build_client)

    tools = mcp_client.get_mcp_tools()

    # Discovery used IMG_PROC_MCP_URL over the http transport.
    assert seen_config["config"]["img-proc"]["url"] == mcp_client.IMG_PROC_MCP_URL
    assert seen_config["config"]["img-proc"]["transport"] == "http"
    # All six image-processing tools were discovered.
    assert {t.name for t in tools} == {
        "rotate",
        "flip",
        "blur",
        "resize",
        "crop",
        "add_noise",
    }


def test_get_mcp_tools_uses_custom_url(monkeypatch):
    # A custom IMG_PROC_MCP_URL flows through to the discovery config.
    monkeypatch.setattr(mcp_client, "IMG_PROC_MCP_URL", "http://mcp-host:9000/mcp")

    seen_config = {}

    def fake_build_client():
        seen_config["config"] = mcp_client._server_config()
        return _FakeClient([_FakeTool("rotate")])

    monkeypatch.setattr(mcp_client, "_build_client", fake_build_client)

    tools = mcp_client.get_mcp_tools()

    assert seen_config["config"]["img-proc"]["url"] == "http://mcp-host:9000/mcp"
    assert [t.name for t in tools] == ["rotate"]


def test_call_mcp_tool_wrapper_removed():
    # The old per-call wrapper is gone: image tools are discovered and bound
    # directly, not invoked one-by-one through a helper.
    assert not hasattr(mcp_client, "call_mcp_tool")
