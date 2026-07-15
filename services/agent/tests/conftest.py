"""Shared fixtures and test helpers for the agent test suite.

This module centralizes the fakes and helpers used across the split test files
(``test_run_agent_*.py`` and ``test_chat_*.py``) so they are defined once:

  * environment setup + app import (must happen before ``import app``),
  * fake LLM / tool / MCP-tool stand-ins for the ReAct loop,
  * message builders and the ``_register_proc`` monkeypatch helper,
  * fake httpx clients and sample detections for the object tools,
  * FastAPI ``client`` and ``clear_chat_state`` fixtures for the /chat tests,
  * the ``_fake_agent_result`` payload and a reusable 1x1 PNG constant.
"""

import io
import json
import os

# Configure the environment BEFORE importing the app module, because app.py
# validates MODEL and builds the LLM client at import time.
os.environ.setdefault("MODEL", "openai.gpt-oss-20b-1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

import app as app_module
from app import app

# A 1x1 PNG pixel, base64 encoded. Reused by the upload/state/follow-up tests.
IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# --- Fakes for the ReAct loop ---------------------------------------------


class FakeLLM:
    """Stand-in for app_module.llm_with_tools.

    Returns a scripted sequence of AIMessage objects on successive .invoke()
    calls, so the ReAct loop never talks to a real LLM.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        # Repeat the last scripted response if the loop runs longer than the
        # script (useful for the max-iterations test).
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


class FakeTool:
    """Stand-in for a LangChain tool: returns a fixed ToolMessage."""

    def __init__(self, result_json):
        self._result_json = result_json

    def invoke(self, tool_call):
        return ToolMessage(
            content=json.dumps(self._result_json),
            tool_call_id=tool_call["id"],
        )


class AsyncProcTool:
    """Stand-in for a discovered async MCP image-processing tool.

    The real MCP tools are async-only and return the processed image's S3 key
    as their text result. This fake mirrors that: it is invoked via `.ainvoke`
    and returns the output key as the ToolMessage content. It also records the
    image key that was in play when it ran, so chaining behaviour can be checked.
    """

    def __init__(self, output_key):
        self.output_key = output_key
        self.seen_input_keys = []

    async def ainvoke(self, tool_call):
        self.seen_input_keys.append(app_module._current_image_s3_key.get())
        return ToolMessage(content=self.output_key, tool_call_id=tool_call["id"])


def _register_proc(monkeypatch, tools: dict):
    """Point ALL_TOOLS at the given fake tools and mark the image-processing
    tool names so run_agent post-processes their output keys.

    run_agent looks tools up from ALL_TOOLS by ``.name``, so each fake is given
    its dict key as its name.
    """
    for name, tool in tools.items():
        tool.name = name
    monkeypatch.setattr(app_module, "ALL_TOOLS", list(tools.values()))
    monkeypatch.setattr(
        app_module,
        "IMG_PROC_TOOL_NAMES",
        {name for name, tool in tools.items() if isinstance(tool, AsyncProcTool)},
    )


def _ai_tool_call(name, call_id, usage=None):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": call_id, "type": "tool_call"}],
        usage_metadata=usage,
    )


def _ai_final(text, usage=None):
    return AIMessage(content=text, usage_metadata=usage)


# --- Fakes for the object tools (detect_objects / select_object) ----------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHTTPClient:
    """Records the request sent by detect_objects and returns a fixed response."""

    def __init__(self, *args, **kwargs):
        self.last_post = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None, **kwargs):
        self.last_post = {"url": url, "json": json}
        _FakeHTTPClient.captured = self.last_post
        return _FakeResponse({"prediction_uid": "abc-123", "labels": ["person"]})


# Two dogs and one cat. Left coordinates: dog A=10, cat=40, dog B=200.
_SAMPLE_DETECTIONS = [
    {"label": "dog", "box": "[10.0, 20.0, 60.0, 90.0]"},
    {"label": "cat", "box": "[40.0, 30.0, 80.0, 100.0]"},
    {"label": "dog", "box": [200.0, 50.0, 260.0, 140.0]},
]


class _FakeGetClient:
    """Fake httpx.Client whose .get() returns a fixed prediction payload."""

    payload = {"detection_objects": _SAMPLE_DETECTIONS}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        _FakeGetClient.captured_url = url
        return _FakeResponse(_FakeGetClient.payload)


def _make_png_bytes(width, height, mode="RGB"):
    from PIL import Image

    image = Image.new(mode, (width, height), color=(255, 0, 0) if mode == "RGB" else (255, 0, 0, 128))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- Fixtures for the /chat endpoint tests --------------------------------


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_chat_state():
    # The backend remembers each conversation's image flow in a module-level
    # store keyed by chat_id. Clear it around every test so state never leaks
    # between tests.
    app_module._chat_state.clear()
    yield
    app_module._chat_state.clear()


def _fake_agent_result():
    return {
        "response": "I found a person and a guitar.",
        "image_url": "http://yolo.example/prediction/abc-123/image",
        "annotated_image": "aGVsbG8=",
        "processed_image": None,
        "prediction_id": "abc-123",
        "latest_image_s3_key": None,
        "latest_image_id": None,
        "original_image_s3_key": None,
        "iterations": 2,
        "tools_called": ["detect_objects", "show_annotated_image"],
        "context_limit_exceeded": False,
        "agent_loop_time_s": 0.42,
        "tokens_used": {"input": 312, "output": 22, "total": 334},
    }
