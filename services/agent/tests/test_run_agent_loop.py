"""ReAct loop control: no-op turns and the max-iterations guard."""

from langchain_core.messages import HumanMessage

import app as app_module
from tests.conftest import FakeLLM, FakeTool, _ai_final, _ai_tool_call, _register_proc


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
    _register_proc(
        monkeypatch, {"detect_objects": FakeTool({"prediction_uid": "x"})}
    )
    monkeypatch.setattr(app_module, "_fetch_annotated_image_b64", lambda uid: None)

    result = app_module.run_agent(
        [HumanMessage(content="Loop forever")], max_iterations=3
    )

    assert result["context_limit_exceeded"] is True
    assert result["iterations"] == 3
    assert result["response"] == "Agent stopped: maximum iterations reached."
