import json
import os

# Configure the environment BEFORE importing the app module, because app.py
# validates MODEL and builds the LLM client at import time.
os.environ.setdefault("MODEL", "openai.gpt-oss-20b-1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import app as app_module

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


def _ai_tool_call(name, call_id, usage=None):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": call_id, "type": "tool_call"}],
        usage_metadata=usage,
    )


def _ai_final(text, usage=None):
    return AIMessage(content=text, usage_metadata=usage)


def test_run_agent_handles_tool_calls(monkeypatch):
    # Script: detect_objects -> show_annotated_image -> final answer.
    responses = [
        _ai_tool_call(
            "detect_objects",
            "call_1",
            usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        ),
        _ai_tool_call(
            "show_annotated_image",
            "call_2",
            usage={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
        ),
        _ai_final(
            "I found a person.",
            usage={"input_tokens": 130, "output_tokens": 4, "total_tokens": 134},
        ),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    fake_tools = {
        "detect_objects": FakeTool({"prediction_uid": "abc-123"}),
        "show_annotated_image": FakeTool(
            {"image_url": "http://yolo.example/prediction/abc-123/image"}
        ),
    }
    monkeypatch.setattr(app_module, "TOOLS", fake_tools)

    # Avoid the real YOLO image download.
    monkeypatch.setattr(
        app_module, "_fetch_annotated_image_b64", lambda uid: "ZmFrZS1pbWFnZQ=="
    )

    result = app_module.run_agent([HumanMessage(content="What is in this image?")])

    assert result["response"] == "I found a person."
    assert result["image_url"] == "http://yolo.example/prediction/abc-123/image"
    assert result["prediction_id"] == "abc-123"
    assert result["annotated_image"] == "ZmFrZS1pbWFnZQ=="
    assert result["tools_called"] == ["detect_objects", "show_annotated_image"]
    assert result["iterations"] == 3
    assert result["context_limit_exceeded"] is False
    # Token usage is summed across every LLM call in the loop.
    assert result["tokens_used"] == {"input": 350, "output": 22, "total": 372}


def test_run_agent_no_tool_calls(monkeypatch):
    app_module._latest_prediction_uid.set(None)

    responses = [
        _ai_final(
            "Hello, how can I help?",
            usage={"input_tokens": 50, "output_tokens": 6, "total_tokens": 56},
        ),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    result = app_module.run_agent([HumanMessage(content="Hi")])

    assert result["response"] == "Hello, how can I help?"
    assert result["image_url"] is None
    assert result["prediction_id"] is None


def test_run_agent_stops_at_max_iterations(monkeypatch):
    # The LLM always asks for a tool, so the loop can only stop on the guard.
    looping_response = _ai_tool_call("detect_objects", "call_loop")
    monkeypatch.setattr(
        app_module, "llm_with_tools", FakeLLM([looping_response])
    )
    monkeypatch.setattr(
        app_module, "TOOLS", {"detect_objects": FakeTool({"prediction_uid": "x"})}
    )
    monkeypatch.setattr(app_module, "_fetch_annotated_image_b64", lambda uid: None)

    result = app_module.run_agent(
        [HumanMessage(content="Loop forever")], max_iterations=3
    )

    assert result["context_limit_exceeded"] is True
    assert result["iterations"] == 3
    assert result["response"] == "Agent stopped: maximum iterations reached."


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


def test_detect_objects_sends_s3_key(monkeypatch):
    # detect_objects must send only the S3 key as JSON, not raw image bytes.
    monkeypatch.setattr(app_module.httpx, "Client", _FakeHTTPClient)

    token = app_module._current_image_s3_key.set("chat-1/pred-1/original/image.jpg")
    try:
        raw = app_module.detect_objects.invoke({})
    finally:
        app_module._current_image_s3_key.reset(token)

    data = json.loads(raw)
    assert data["prediction_uid"] == "abc-123"
    assert _FakeHTTPClient.captured["json"] == {
        "image_s3_key": "chat-1/pred-1/original/image.jpg"
    }
    assert _FakeHTTPClient.captured["url"].endswith("/predict")


def test_detect_objects_without_image_returns_error():
    token = app_module._current_image_s3_key.set(None)
    try:
        raw = app_module.detect_objects.invoke({})
    finally:
        app_module._current_image_s3_key.reset(token)

    assert json.loads(raw) == {"error": "No image was provided by the user."}

