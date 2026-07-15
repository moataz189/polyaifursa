"""Token accounting: usage_metadata is summed across every LLM call."""

from langchain_core.messages import HumanMessage

import app as app_module
from tests.conftest import FakeLLM, FakeTool, _ai_final, _ai_tool_call, _register_proc


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
    _register_proc(monkeypatch, fake_tools)

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
