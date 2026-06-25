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
    assert result["annotated_image"] is None
    assert result["tools_called"] == []
    assert result["iterations"] == 1
    assert result["context_limit_exceeded"] is False


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
