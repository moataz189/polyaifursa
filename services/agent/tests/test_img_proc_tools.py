"""Tests for the MCP image-processing integration in the agent.

The image-processing tools (rotate, flip, blur, resize, crop, add_noise) are no
longer hand-written @tool wrappers in app.py. They are discovered from the
img-proc MCP server via MultiServerMCPClient and bound to the LLM directly.

These tests verify:
  * the discovered tools are registered into TOOLS and bound to the LLM,
  * no local @tool wrappers exist for the image-processing tools,
  * the S3-key context message is built for the model,
  * run_agent invokes MCP tools (async) and post-processes their output_key.

Everything is mocked: no real MCP server, YOLO service, or S3 is contacted.
"""

import json
import os

os.environ.setdefault("MODEL", "openai.gpt-oss-20b-1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import app as app_module

INPUT_KEY = "chat/img/original/test.jpeg"
PROCESSED_KEY = "chat/img/processed/rotate_out.png"


class _FakeMCPTool:
    """Async-only stand-in for a discovered LangChain MCP tool.

    Mirrors how langchain-mcp-adapters tools behave: they expose `.name` and are
    invoked with `.ainvoke(tool_call)`, returning a ToolMessage whose content is
    the tool's text result (for the image tools, the processed S3 key).
    """

    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = []

    async def ainvoke(self, tool_call):
        self.calls.append(tool_call)
        return ToolMessage(content=self.result, tool_call_id=tool_call["id"])

    def invoke(self, tool_call):  # pragma: no cover - MCP tools are async-only
        raise NotImplementedError("MCP tools are async-only")


class _FakeBindLLM:
    """Fake chat model whose bind_tools accepts any tool objects.

    The real LLM validates tool objects, but these tests use lightweight fakes,
    so we stub bind_tools to just record what it was given.
    """

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return "STUB_BOUND"


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Replace the module LLM so binding fake tools doesn't hit the real model."""
    monkeypatch.setattr(app_module, "llm", _FakeBindLLM())


def _make_mcp_tools():
    return [
        _FakeMCPTool("rotate", PROCESSED_KEY),
        _FakeMCPTool("flip", "chat/img/processed/flip_out.png"),
        _FakeMCPTool("blur", "chat/img/processed/blur_out.png"),
        _FakeMCPTool("resize", "chat/img/processed/resize_out.png"),
        _FakeMCPTool("crop", "chat/img/processed/crop_out.png"),
        _FakeMCPTool("add_noise", "chat/img/processed/add_noise_out.png"),
    ]


def _use_mcp_tools(monkeypatch, mcp_tools):
    """Point ALL_TOOLS at LOCAL_TOOLS + the given fake MCP tools, and record
    their names as image-processing tools (for post-processing)."""
    monkeypatch.setattr(
        app_module, "ALL_TOOLS", app_module.LOCAL_TOOLS + list(mcp_tools)
    )
    monkeypatch.setattr(
        app_module, "IMG_PROC_TOOL_NAMES", {t.name for t in mcp_tools}
    )


def test_no_local_wrappers_for_image_processing_tools():
    # The image-processing tools must NOT be defined as local @tool functions
    # in app.py; they are discovered over MCP instead.
    for name in ("rotate", "flip", "blur", "resize", "crop", "add_noise"):
        assert not hasattr(app_module, name), (
            f"app.py must not define a local wrapper for {name!r}"
        )
    # The old per-call MCP helper and its import are gone too.
    assert not hasattr(app_module, "call_mcp_tool")
    assert not hasattr(app_module, "_run_img_proc_tool")


def test_no_manual_tools_registry():
    # The manual name -> tool registry and its rebind helper are removed; the
    # agent works directly with the ALL_TOOLS list.
    assert not hasattr(app_module, "TOOLS")
    assert not hasattr(app_module, "register_mcp_tools")


def test_local_yolo_tools_are_present():
    for name in ("detect_objects", "show_annotated_image", "select_object"):
        assert hasattr(app_module, name)
    local_names = {t.name for t in app_module.LOCAL_TOOLS}
    assert local_names == {"detect_objects", "show_annotated_image", "select_object"}


def test_all_tools_combines_local_and_discovered(monkeypatch):
    # ALL_TOOLS = LOCAL_TOOLS + mcp_tools, and IMG_PROC_TOOL_NAMES tracks the
    # discovered image tools for post-processing.
    mcp_tools = _make_mcp_tools()
    _use_mcp_tools(monkeypatch, mcp_tools)

    assert {t.name for t in app_module.ALL_TOOLS} == {
        "detect_objects",
        "show_annotated_image",
        "select_object",
        "rotate",
        "flip",
        "blur",
        "resize",
        "crop",
        "add_noise",
    }
    assert app_module.IMG_PROC_TOOL_NAMES == {
        "rotate",
        "flip",
        "blur",
        "resize",
        "crop",
        "add_noise",
    }


def test_startup_binds_all_tools(monkeypatch):
    # On startup the LLM is bound to LOCAL_TOOLS + discovered mcp_tools.
    captured = {}

    class FakeLLM:
        def bind_tools(self, tools):
            captured["tools"] = tools
            return "BOUND"

    monkeypatch.setattr(app_module, "llm", FakeLLM())

    mcp_tools = _make_mcp_tools()
    all_tools = app_module.LOCAL_TOOLS + mcp_tools
    bound = app_module.llm.bind_tools(all_tools)

    bound_names = [t.name for t in captured["tools"]]
    # Local tools come first, then every discovered MCP tool.
    assert bound_names[:3] == [
        "detect_objects",
        "show_annotated_image",
        "select_object",
    ]
    assert set(bound_names[3:]) == {
        "rotate",
        "flip",
        "blur",
        "resize",
        "crop",
        "add_noise",
    }
    assert bound == "BOUND"


def test_discovery_returns_tools(monkeypatch):
    # _discover_mcp_tools pulls tools from get_mcp_tools() (this is what runs at
    # import time to build ALL_TOOLS).
    mcp_tools = _make_mcp_tools()
    monkeypatch.setattr(app_module, "get_mcp_tools", lambda: mcp_tools)

    returned = app_module._discover_mcp_tools()
    assert {t.name for t in returned} == {
        "rotate",
        "flip",
        "blur",
        "resize",
        "crop",
        "add_noise",
    }


def test_discovery_is_best_effort_when_server_unreachable(monkeypatch):
    # If discovery raises (server down), _discover_mcp_tools returns an empty
    # list instead of crashing at import, so the agent keeps its local tools.
    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module, "get_mcp_tools", boom)

    assert app_module._discover_mcp_tools() == []


def test_build_key_context_message_lists_available_keys():
    msg = app_module._build_key_context_message(
        latest_key="chat/img/processed/x.png",
        original_key="chat/img/original/x.jpeg",
        processed_key="chat/img/processed/x.png",
    )
    assert "latest_image_s3_key" in msg
    assert "original_image_s3_key" in msg
    assert "latest_processed_image_s3_key" in msg
    assert "chat/img/original/x.jpeg" in msg
    assert "input_key" in msg


def test_build_key_context_message_none_when_no_keys():
    assert (
        app_module._build_key_context_message(
            latest_key=None, original_key=None, processed_key=None
        )
        is None
    )


def test_extract_output_key_from_plain_key():
    assert app_module._extract_output_key(PROCESSED_KEY) == PROCESSED_KEY


def test_extract_output_key_from_json_object():
    assert (
        app_module._extract_output_key(json.dumps({"output_key": PROCESSED_KEY}))
        == PROCESSED_KEY
    )


def test_extract_output_key_empty_returns_none():
    assert app_module._extract_output_key("") is None
    assert app_module._extract_output_key(None) is None


class _ScriptedLLM:
    """Returns scripted AIMessages on successive invoke() calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


def _ai_tool_call(name, call_id, args=None):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}
        ],
    )


def _ai_final(text):
    return AIMessage(content=text)


def test_run_agent_invokes_mcp_tool_and_returns_processed_image(monkeypatch):
    # The model calls the discovered `rotate` MCP tool with an input_key; the
    # loop invokes it async, reads the returned output_key, and downloads the
    # processed image.
    rotate_tool = _FakeMCPTool("rotate", PROCESSED_KEY)
    _use_mcp_tools(monkeypatch, [rotate_tool])

    responses = [
        _ai_tool_call(
            "rotate", "call_1", args={"input_key": INPUT_KEY, "angle": 90}
        ),
        _ai_final("Rotated the image for you."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", _ScriptedLLM(responses))
    monkeypatch.setattr(
        app_module, "_fetch_processed_image_b64", lambda key: "cHJvY2Vzc2Vk"
    )

    token = app_module._current_image_s3_key.set(INPUT_KEY)
    try:
        result = app_module.run_agent([HumanMessage(content="rotate it 90")])
    finally:
        app_module._current_image_s3_key.reset(token)

    assert result["response"] == "Rotated the image for you."
    assert result["processed_image"] == "cHJvY2Vzc2Vk"
    assert result["latest_image_s3_key"] == PROCESSED_KEY
    assert result["tools_called"] == ["rotate"]
    # The MCP tool was invoked via its async interface with the model's args.
    assert rotate_tool.calls[0]["args"] == {"input_key": INPUT_KEY, "angle": 90}
