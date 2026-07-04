"""Tests for the image-processing MCP tools wired into the agent.

The MCP transport is fully mocked here: `call_mcp_tool` is replaced so no real
MCP server subprocess is ever started. These tests verify the LangChain tool
wrappers pass the right arguments, inject the current image key from context,
return the processed S3 key, and are registered on the agent.
"""

import json
import os

os.environ.setdefault("MODEL", "openai.gpt-oss-20b-1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")

import pytest

import app as app_module
from app import add_noise, blur, crop, flip, resize, rotate

INPUT_KEY = "chat/pred/original/test.jpeg"


@pytest.fixture
def mock_mcp(monkeypatch):
    """Replace call_mcp_tool with a fake that records calls and returns a key.

    Also seeds the current-image context var so the wrappers have an input key.
    """
    calls = []

    def fake_call(name, arguments):
        calls.append((name, arguments))
        return f"chat/pred/processed/{name}_test.png"

    monkeypatch.setattr(app_module, "call_mcp_tool", fake_call)
    token = app_module._current_image_s3_key.set(INPUT_KEY)
    yield calls
    app_module._current_image_s3_key.reset(token)


def _invoke(tool, args):
    """Invoke a LangChain tool and return its parsed JSON result."""
    return json.loads(tool.invoke(args))


def test_all_img_proc_tools_registered():
    for name in ("rotate", "flip", "blur", "resize", "crop", "add_noise"):
        assert name in app_module.TOOLS
    # YOLO tools remain registered.
    assert "detect_objects" in app_module.TOOLS
    assert "show_annotated_image" in app_module.TOOLS


def test_rotate_calls_mcp_and_returns_key(mock_mcp):
    result = _invoke(rotate, {"angle": 90})
    assert result["output_key"] == "chat/pred/processed/rotate_test.png"
    assert mock_mcp == [("rotate", {"input_key": INPUT_KEY, "angle": 90})]


def test_flip_calls_mcp_and_returns_key(mock_mcp):
    result = _invoke(flip, {"direction": "vertical"})
    assert result["output_key"] == "chat/pred/processed/flip_test.png"
    assert mock_mcp == [("flip", {"input_key": INPUT_KEY, "direction": "vertical"})]


def test_blur_calls_mcp_and_returns_key(mock_mcp):
    result = _invoke(blur, {"radius": 3.0})
    assert result["output_key"] == "chat/pred/processed/blur_test.png"
    assert mock_mcp == [("blur", {"input_key": INPUT_KEY, "radius": 3.0})]


def test_resize_calls_mcp_and_returns_key(mock_mcp):
    result = _invoke(resize, {"width": 800, "height": 600})
    assert result["output_key"] == "chat/pred/processed/resize_test.png"
    assert mock_mcp == [
        ("resize", {"input_key": INPUT_KEY, "width": 800, "height": 600})
    ]


def test_crop_calls_mcp_and_returns_key(mock_mcp):
    result = _invoke(crop, {"left": 10, "top": 20, "right": 100, "bottom": 150})
    assert result["output_key"] == "chat/pred/processed/crop_test.png"
    assert mock_mcp == [
        (
            "crop",
            {"input_key": INPUT_KEY, "left": 10, "top": 20, "right": 100, "bottom": 150},
        )
    ]


def test_add_noise_calls_mcp_and_returns_key(mock_mcp):
    result = _invoke(add_noise, {"amount": 0.05})
    assert result["output_key"] == "chat/pred/processed/add_noise_test.png"
    assert mock_mcp == [("add_noise", {"input_key": INPUT_KEY, "amount": 0.05})]


def test_tool_without_image_returns_error(monkeypatch):
    # No current image set -> the wrapper should short-circuit with an error and
    # never touch the MCP transport.
    called = []
    monkeypatch.setattr(
        app_module, "call_mcp_tool", lambda *a, **k: called.append(a) or "x"
    )
    token = app_module._current_image_s3_key.set(None)
    try:
        result = _invoke(rotate, {"angle": 90})
    finally:
        app_module._current_image_s3_key.reset(token)

    assert "error" in result
    assert called == []


def test_tool_surfaces_mcp_error(monkeypatch):
    def boom(name, arguments):
        raise RuntimeError("direction must be 'horizontal' or 'vertical'")

    monkeypatch.setattr(app_module, "call_mcp_tool", boom)
    token = app_module._current_image_s3_key.set(INPUT_KEY)
    try:
        result = _invoke(flip, {"direction": "diagonal"})
    finally:
        app_module._current_image_s3_key.reset(token)

    assert result["error"] == "direction must be 'horizontal' or 'vertical'"
