import json
import os

# Configure the environment BEFORE importing the app module, because app.py
# validates MODEL and builds the LLM client at import time.
os.environ.setdefault("MODEL", "openai.gpt-oss-20b-1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")

import pytest
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


def test_run_agent_returns_processed_image(monkeypatch):
    app_module._latest_prediction_uid.set(None)

    # Script: rotate (image-processing tool) -> final answer.
    responses = [
        _ai_tool_call("rotate", "call_1"),
        _ai_final("Rotated your image."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    output_key = "chat/pred/processed/rotate_90_test.png"
    monkeypatch.setattr(
        app_module, "TOOLS", {"rotate": FakeTool({"output_key": output_key})}
    )

    # The processed image is downloaded from S3 by its output key and encoded.
    downloaded = {}

    def fake_download(key):
        downloaded["key"] = key
        return b"processed-bytes"

    monkeypatch.setattr(app_module, "download_image", fake_download)

    result = app_module.run_agent([HumanMessage(content="rotate 90 degrees")])

    assert result["response"] == "Rotated your image."
    assert downloaded["key"] == output_key
    # base64 of b"processed-bytes"
    assert result["processed_image"] == "cHJvY2Vzc2VkLWJ5dGVz"
    assert result["tools_called"] == ["rotate"]
    # The processed image's key becomes the latest usable image key so it can
    # round-trip to the client for the next request.
    assert result["latest_image_s3_key"] == output_key


def test_run_agent_reuses_current_image_key_for_detect(monkeypatch):
    # A follow-up request without a new image: the context var already holds the
    # key carried over from a previous request. detect_objects must operate on
    # that key, and it round-trips back in latest_image_s3_key.
    prior_key = "chat/pred/original/pic.png"
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_s3_key.set(prior_key)

    responses = [
        _ai_tool_call("detect_objects", "call_1"),
        _ai_final("I found a person."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    seen = {}

    class RecordingTool:
        def invoke(self, tool_call):
            seen["image_key"] = app_module._current_image_s3_key.get()
            return ToolMessage(
                content=json.dumps({"prediction_uid": "uid-1"}),
                tool_call_id=tool_call["id"],
            )

    monkeypatch.setattr(app_module, "TOOLS", {"detect_objects": RecordingTool()})

    result = app_module.run_agent([HumanMessage(content="analyze the previous image")])

    assert seen["image_key"] == prior_key
    assert result["latest_image_s3_key"] == prior_key
    assert result["prediction_id"] == "uid-1"


def test_run_agent_chains_processing_tools(monkeypatch):
    # rotate then blur: the second tool must operate on the FIRST tool's output
    # key, and the final latest_image_s3_key is the last output key.
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_s3_key.set("chat/pred/original/pic.png")

    responses = [
        _ai_tool_call("rotate", "call_1"),
        _ai_tool_call("blur", "call_2"),
        _ai_final("Done."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    seen_inputs = []

    class ProcTool:
        def __init__(self, output_key):
            self._output_key = output_key

        def invoke(self, tool_call):
            seen_inputs.append(app_module._current_image_s3_key.get())
            return ToolMessage(
                content=json.dumps({"output_key": self._output_key}),
                tool_call_id=tool_call["id"],
            )

    monkeypatch.setattr(
        app_module,
        "TOOLS",
        {
            "rotate": ProcTool("chat/pred/processed/rotate_test.png"),
            "blur": ProcTool("chat/pred/processed/blur_test.png"),
        },
    )
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")

    result = app_module.run_agent([HumanMessage(content="rotate then blur")])

    # rotate saw the original; blur saw rotate's output.
    assert seen_inputs == [
        "chat/pred/original/pic.png",
        "chat/pred/processed/rotate_test.png",
    ]
    assert result["latest_image_s3_key"] == "chat/pred/processed/blur_test.png"


def test_run_agent_processing_resets_prediction(monkeypatch):
    # detect image A, then process it (image B). The prediction must be reset,
    # because the processed image has no YOLO detection yet.
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_s3_key.set("chat/pred/original/A.png")

    responses = [
        _ai_tool_call("detect_objects", "call_1"),
        _ai_tool_call("blur", "call_2"),
        _ai_final("Detected then blurred."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    fake_tools = {
        "detect_objects": FakeTool({"prediction_uid": "predA"}),
        "blur": FakeTool({"output_key": "chat/pred/processed/blur_B.png"}),
    }
    monkeypatch.setattr(app_module, "TOOLS", fake_tools)
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")

    result = app_module.run_agent([HumanMessage(content="detect then blur")])

    # The processed image is now the latest, and the prediction is cleared.
    assert result["latest_image_s3_key"] == "chat/pred/processed/blur_B.png"
    assert result["prediction_id"] is None


def test_run_agent_show_annotated_blocked_after_processing(monkeypatch):
    # Scenario: detect image A, process to image B, then ask to show the
    # annotated image. It must NOT return image A's annotated result.
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_s3_key.set("chat/pred/original/A.png")

    responses = [
        _ai_tool_call("detect_objects", "call_1"),
        _ai_tool_call("blur", "call_2"),
        _ai_tool_call("show_annotated_image", "call_3"),
        _ai_final("Here you go."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    fake_tools = {
        "detect_objects": FakeTool({"prediction_uid": "predA"}),
        "blur": FakeTool({"output_key": "chat/pred/processed/blur_B.png"}),
        "show_annotated_image": FakeTool(
            {"image_url": "http://yolo.example/prediction/predA/image"}
        ),
    }
    monkeypatch.setattr(app_module, "TOOLS", fake_tools)
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")

    # If the guard fails, this would be called and return A's image.
    monkeypatch.setattr(
        app_module, "_fetch_annotated_image_b64", lambda uid: "QQ=="  # base64("A")
    )

    result = app_module.run_agent([HumanMessage(content="detect, blur, show annotated")])

    # The stale prediction (image A) must not be surfaced for image B.
    assert result["annotated_image"] is None
    assert result["image_url"] is None
    assert result["prediction_id"] is None
    assert result["latest_image_s3_key"] == "chat/pred/processed/blur_B.png"


def test_run_agent_no_processed_image_without_img_proc_tool(monkeypatch):
    app_module._latest_prediction_uid.set(None)

    responses = [_ai_final("Just chatting.")]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    result = app_module.run_agent([HumanMessage(content="hi")])

    assert result["processed_image"] is None


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


def test_detect_objects_current_uses_latest_key(monkeypatch):
    # source="current" (the default) detects the latest usable image key.
    monkeypatch.setattr(app_module.httpx, "Client", _FakeHTTPClient)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/processed/rot_pic.png")
    t_orig = app_module._original_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_proc = app_module._latest_processed_key.set("chat-1/img-1/processed/rot_pic.png")
    try:
        raw = app_module.detect_objects.invoke({})
    finally:
        app_module._current_image_s3_key.reset(t_cur)
        app_module._original_image_s3_key.reset(t_orig)
        app_module._latest_processed_key.reset(t_proc)

    data = json.loads(raw)
    assert _FakeHTTPClient.captured["json"] == {
        "image_s3_key": "chat-1/img-1/processed/rot_pic.png"
    }
    assert data["detected_image_s3_key"] == "chat-1/img-1/processed/rot_pic.png"


def test_detect_objects_original_uses_original_key(monkeypatch):
    # source="original" detects the originally uploaded image, ignoring any
    # processing that happened afterwards.
    monkeypatch.setattr(app_module.httpx, "Client", _FakeHTTPClient)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/processed/rot_pic.png")
    t_orig = app_module._original_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_proc = app_module._latest_processed_key.set("chat-1/img-1/processed/rot_pic.png")
    try:
        raw = app_module.detect_objects.invoke({"source": "original"})
    finally:
        app_module._current_image_s3_key.reset(t_cur)
        app_module._original_image_s3_key.reset(t_orig)
        app_module._latest_processed_key.reset(t_proc)

    data = json.loads(raw)
    assert _FakeHTTPClient.captured["json"] == {
        "image_s3_key": "chat-1/img-1/original/pic.png"
    }
    assert data["detected_image_s3_key"] == "chat-1/img-1/original/pic.png"


def test_detect_objects_processed_uses_latest_processed_key(monkeypatch):
    # source="processed" detects the most recent processed image (e.g. after a
    # rotate), which differs from the original upload.
    monkeypatch.setattr(app_module.httpx, "Client", _FakeHTTPClient)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_orig = app_module._original_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_proc = app_module._latest_processed_key.set("chat-1/img-1/processed/rot_pic.png")
    try:
        raw = app_module.detect_objects.invoke({"source": "processed"})
    finally:
        app_module._current_image_s3_key.reset(t_cur)
        app_module._original_image_s3_key.reset(t_orig)
        app_module._latest_processed_key.reset(t_proc)

    data = json.loads(raw)
    assert _FakeHTTPClient.captured["json"] == {
        "image_s3_key": "chat-1/img-1/processed/rot_pic.png"
    }


def test_img_proc_original_uses_original_key(monkeypatch):
    # add_noise(source="original") must process the ORIGINAL image key, not the
    # current/processed one.
    called = {}

    def fake_call_mcp_tool(name, args):
        called["name"] = name
        called["args"] = args
        return "chat-1/img-1/processed/add_noise_pic.png"

    monkeypatch.setattr(app_module, "call_mcp_tool", fake_call_mcp_tool)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/processed/rot_pic.png")
    t_orig = app_module._original_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_proc = app_module._latest_processed_key.set("chat-1/img-1/processed/rot_pic.png")
    try:
        raw = app_module.add_noise.invoke({"amount": 0.05, "source": "original"})
    finally:
        app_module._current_image_s3_key.reset(t_cur)
        app_module._original_image_s3_key.reset(t_orig)
        app_module._latest_processed_key.reset(t_proc)

    data = json.loads(raw)
    assert data["output_key"] == "chat-1/img-1/processed/add_noise_pic.png"
    assert called["name"] == "add_noise"
    # The MCP tool received the ORIGINAL key, not the current/processed key.
    assert called["args"]["input_key"] == "chat-1/img-1/original/pic.png"
    assert called["args"]["amount"] == 0.05


def test_img_proc_current_uses_latest_key(monkeypatch):
    # rotate(source="current") (the default) processes the latest usable image.
    called = {}

    def fake_call_mcp_tool(name, args):
        called["args"] = args
        return "chat-1/img-1/processed/rotate_90_rot_pic.png"

    monkeypatch.setattr(app_module, "call_mcp_tool", fake_call_mcp_tool)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/processed/rot_pic.png")
    t_orig = app_module._original_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_proc = app_module._latest_processed_key.set("chat-1/img-1/processed/rot_pic.png")
    try:
        raw = app_module.rotate.invoke({"angle": 90})
    finally:
        app_module._current_image_s3_key.reset(t_cur)
        app_module._original_image_s3_key.reset(t_orig)
        app_module._latest_processed_key.reset(t_proc)

    json.loads(raw)
    # Default source="current" uses the latest usable image key.
    assert called["args"]["input_key"] == "chat-1/img-1/processed/rot_pic.png"
    assert called["args"]["angle"] == 90


def test_img_proc_processed_uses_latest_processed_key(monkeypatch):
    # blur(source="processed") processes the most recent processed image.
    called = {}

    def fake_call_mcp_tool(name, args):
        called["args"] = args
        return "chat-1/img-1/processed/blur_rot_pic.png"

    monkeypatch.setattr(app_module, "call_mcp_tool", fake_call_mcp_tool)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_orig = app_module._original_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_proc = app_module._latest_processed_key.set("chat-1/img-1/processed/rot_pic.png")
    try:
        raw = app_module.blur.invoke({"radius": 3.0, "source": "processed"})
    finally:
        app_module._current_image_s3_key.reset(t_cur)
        app_module._original_image_s3_key.reset(t_orig)
        app_module._latest_processed_key.reset(t_proc)

    json.loads(raw)
    assert called["args"]["input_key"] == "chat-1/img-1/processed/rot_pic.png"


# --- Object-specific selection --------------------------------------------

# Two dogs and one cat. Left coordinates: dog A=10, cat=40, dog B=200.
_SAMPLE_DETECTIONS = [
    {"label": "dog", "box": "[10.0, 20.0, 60.0, 90.0]"},
    {"label": "cat", "box": "[40.0, 30.0, 80.0, 100.0]"},
    {"label": "dog", "box": [200.0, 50.0, 260.0, 140.0]},
]


def test_select_object_bbox_second_dog_from_right():
    # Dogs by left coordinate: A(left=10), B(left=200). from_right -> [B, A].
    # The SECOND dog from the right is dog A at left=10.
    box = app_module.select_object_bbox(
        _SAMPLE_DETECTIONS, label="dog", index=2, direction="from_right"
    )
    assert box == {"left": 10, "top": 20, "right": 60, "bottom": 90}


def test_select_object_bbox_first_dog_from_right():
    box = app_module.select_object_bbox(
        _SAMPLE_DETECTIONS, label="dog", index=1, direction="from_right"
    )
    assert box == {"left": 200, "top": 50, "right": 260, "bottom": 140}


def test_select_object_bbox_first_dog_from_left():
    box = app_module.select_object_bbox(
        _SAMPLE_DETECTIONS, label="dog", index=1, direction="from_left"
    )
    assert box == {"left": 10, "top": 20, "right": 60, "bottom": 90}


def test_select_object_bbox_invalid_raises():
    # Unknown label.
    with pytest.raises(ValueError):
        app_module.select_object_bbox(_SAMPLE_DETECTIONS, label="car")
    # Index out of range (only two dogs).
    with pytest.raises(ValueError):
        app_module.select_object_bbox(_SAMPLE_DETECTIONS, label="dog", index=3)
    # Bad direction.
    with pytest.raises(ValueError):
        app_module.select_object_bbox(_SAMPLE_DETECTIONS, label="dog", direction="up")


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


def test_select_object_tool_second_dog_from_right(monkeypatch):
    monkeypatch.setattr(app_module.httpx, "Client", _FakeGetClient)
    app_module._latest_prediction_uid.set("pred-xyz")

    raw = app_module.select_object.invoke(
        {"label": "dog", "index": 2, "direction": "from_right"}
    )

    box = json.loads(raw)
    assert box == {"left": 10, "top": 20, "right": 60, "bottom": 90}
    assert _FakeGetClient.captured_url.endswith("/prediction/pred-xyz")


def test_select_object_tool_without_detection_errors(monkeypatch):
    app_module._latest_prediction_uid.set(None)
    raw = app_module.select_object.invoke({"label": "dog"})
    assert "error" in json.loads(raw)


def test_blur_with_bbox_forwards_region_coords(monkeypatch):
    # blur with a bounding box must forward the coordinates to the MCP tool so
    # only that region is blurred.
    called = {}

    def fake_call_mcp_tool(name, args):
        called["name"] = name
        called["args"] = args
        return "chat-1/img-1/processed/blur_box_pic.png"

    monkeypatch.setattr(app_module, "call_mcp_tool", fake_call_mcp_tool)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_orig = app_module._original_image_s3_key.set("chat-1/img-1/original/pic.png")
    t_proc = app_module._latest_processed_key.set(None)
    try:
        app_module.blur.invoke(
            {"radius": 4.0, "left": 10, "top": 20, "right": 60, "bottom": 90}
        )
    finally:
        app_module._current_image_s3_key.reset(t_cur)
        app_module._original_image_s3_key.reset(t_orig)
        app_module._latest_processed_key.reset(t_proc)

    assert called["name"] == "blur"
    assert called["args"]["left"] == 10
    assert called["args"]["top"] == 20
    assert called["args"]["right"] == 60
    assert called["args"]["bottom"] == 90


def test_blur_without_bbox_omits_region_coords(monkeypatch):
    # Whole-image blur must NOT send any bounding-box coordinates.
    called = {}

    def fake_call_mcp_tool(name, args):
        called["args"] = args
        return "chat-1/img-1/processed/blur_pic.png"

    monkeypatch.setattr(app_module, "call_mcp_tool", fake_call_mcp_tool)

    t_cur = app_module._current_image_s3_key.set("chat-1/img-1/original/pic.png")
    try:
        app_module.blur.invoke({"radius": 2.0})
    finally:
        app_module._current_image_s3_key.reset(t_cur)

    assert "left" not in called["args"]
    assert "top" not in called["args"]
    assert "right" not in called["args"]
    assert "bottom" not in called["args"]


def test_run_agent_detect_original_after_noise_uses_original_key(monkeypatch):
    # Simulate the real flow within a single loop: add_noise changes the latest
    # image key to a processed/noisy key, but detect_objects(source="original")
    # must still send the ORIGINAL key to YOLO, NOT the noisy one.
    original_key = "chat/img-1/original/pic.png"
    noisy_key = "chat/img-1/processed/add_noise_pic.png"

    app_module._latest_prediction_uid.set(None)
    app_module._current_image_id.set("img-1")
    app_module._current_image_s3_key.set(original_key)
    app_module._original_image_s3_key.set(original_key)
    app_module._latest_processed_key.set(None)

    responses = [
        _ai_tool_call("add_noise", "call_1"),
        _ai_tool_call("detect_objects", "call_2"),
        _ai_final("Found a person in the original image."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    detected = {}

    class RecordingDetect:
        def invoke(self, tool_call):
            # detect_objects(source="original") resolves via the context vars,
            # so record which key it would send to YOLO.
            key = app_module._resolve_detect_source("original")
            detected["key"] = key
            return ToolMessage(
                content=json.dumps(
                    {"prediction_uid": "uid-1", "detected_image_s3_key": key}
                ),
                tool_call_id=tool_call["id"],
            )

    monkeypatch.setattr(
        app_module,
        "TOOLS",
        {
            "add_noise": FakeTool({"output_key": noisy_key}),
            "detect_objects": RecordingDetect(),
        },
    )
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")

    result = app_module.run_agent(
        [HumanMessage(content="add noise then detect the original image")]
    )

    # After add_noise, the latest usable key is the noisy key...
    assert result["latest_image_s3_key"] == noisy_key
    # ...but detecting "original" used the ORIGINAL key, not the noisy one.
    assert detected["key"] == original_key
    assert detected["key"] != noisy_key
    # The original key round-trips unchanged.
    assert result["original_image_s3_key"] == original_key


def test_run_agent_returns_image_id(monkeypatch):
    # run_agent round-trips the image_id of the current flow back to the caller.
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_id.set("img-123")

    responses = [_ai_final("Hello.")]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    result = app_module.run_agent([HumanMessage(content="hi")])

    assert result["latest_image_id"] == "img-123"


def test_run_agent_rotate_keeps_image_id(monkeypatch):
    # Rotating keeps the SAME image_id (same image flow) while changing the
    # latest usable image key to the processed output.
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_id.set("img-123")
    app_module._current_image_s3_key.set("chat/img-123/original/pic.png")
    app_module._original_image_s3_key.set("chat/img-123/original/pic.png")
    app_module._latest_processed_key.set(None)

    responses = [
        _ai_tool_call("rotate", "call_1"),
        _ai_final("Rotated."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    output_key = "chat/img-123/processed/rotate_90_pic.png"
    monkeypatch.setattr(
        app_module, "TOOLS", {"rotate": FakeTool({"output_key": output_key})}
    )
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")

    result = app_module.run_agent([HumanMessage(content="rotate 90")])

    # image_id is unchanged, but the latest usable key is the processed output.
    assert result["latest_image_id"] == "img-123"
    assert result["latest_image_s3_key"] == output_key


def test_run_agent_detect_after_rotate_uid_differs_from_image_id(monkeypatch):
    # After a rotate, detecting the processed image creates a prediction_uid
    # that is different from the image_id.
    image_id = "img-123"
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_id.set(image_id)
    app_module._current_image_s3_key.set("chat/img-123/processed/rotate_90_pic.png")
    app_module._original_image_s3_key.set("chat/img-123/original/pic.png")
    app_module._latest_processed_key.set("chat/img-123/processed/rotate_90_pic.png")

    responses = [
        _ai_tool_call("detect_objects", "call_1"),
        _ai_final("Found a person."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))
    monkeypatch.setattr(
        app_module,
        "TOOLS",
        {"detect_objects": FakeTool({"prediction_uid": "pred-uid-999"})},
    )

    result = app_module.run_agent([HumanMessage(content="detect the rotated image")])

    assert result["prediction_id"] == "pred-uid-999"
    assert result["prediction_id"] != image_id
    assert result["latest_image_id"] == image_id


def test_run_agent_detect_original_then_show_annotated_returns_image(monkeypatch):
    # "Detect objects in the original image and show the annotated image" while
    # the latest image in play is a PROCESSED image. The prediction is created
    # THIS run on the original source, so show_annotated_image must still return
    # the annotated image even though prediction_image_key != latest_image_s3_key.
    original_key = "chat/img-1/original/pic.png"
    processed_key = "chat/img-1/processed/rotate_90_pic.png"

    app_module._latest_prediction_uid.set(None)
    app_module._current_image_id.set("img-1")
    # The latest image in play is the processed one, NOT the original.
    app_module._current_image_s3_key.set(processed_key)
    app_module._original_image_s3_key.set(original_key)
    app_module._latest_processed_key.set(processed_key)

    responses = [
        _ai_tool_call("detect_objects", "call_1"),
        _ai_tool_call("show_annotated_image", "call_2"),
        _ai_final("Here is the annotated original image."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    fake_tools = {
        # detect_objects ran on the ORIGINAL source, so it echoes the original key.
        "detect_objects": FakeTool(
            {"prediction_uid": "uid-1", "detected_image_s3_key": original_key}
        ),
        "show_annotated_image": FakeTool(
            {"image_url": "http://yolo.example/prediction/uid-1/image"}
        ),
    }
    monkeypatch.setattr(app_module, "TOOLS", fake_tools)
    monkeypatch.setattr(
        app_module, "_fetch_annotated_image_b64", lambda uid: "YW5ub3RhdGVk"
    )

    result = app_module.run_agent(
        [HumanMessage(content="detect objects in the original image and show the annotated image")]
    )

    assert result["tools_called"] == ["detect_objects", "show_annotated_image"]
    assert result["prediction_id"] == "uid-1"
    # The annotated image is returned even though the latest image is processed.
    assert result["annotated_image"] == "YW5ub3RhdGVk"
    assert result["image_url"] == "http://yolo.example/prediction/uid-1/image"


def test_run_agent_processing_after_detect_resets_annotation(monkeypatch):
    # After a detect (created this run), a subsequent image-processing tool
    # resets the prediction, so a later show_annotated_image must NOT surface
    # the now-stale annotation.
    app_module._latest_prediction_uid.set(None)
    app_module._current_image_id.set("img-1")
    app_module._current_image_s3_key.set("chat/img-1/original/pic.png")
    app_module._original_image_s3_key.set("chat/img-1/original/pic.png")
    app_module._latest_processed_key.set(None)

    responses = [
        _ai_tool_call("detect_objects", "call_1"),
        _ai_tool_call("rotate", "call_2"),
        _ai_tool_call("show_annotated_image", "call_3"),
        _ai_final("done"),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    fake_tools = {
        "detect_objects": FakeTool({"prediction_uid": "uid-1"}),
        "rotate": FakeTool({"output_key": "chat/img-1/processed/rotate_90_pic.png"}),
        "show_annotated_image": FakeTool(
            {"image_url": "http://yolo.example/prediction/uid-1/image"}
        ),
    }
    monkeypatch.setattr(app_module, "TOOLS", fake_tools)
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")
    monkeypatch.setattr(
        app_module, "_fetch_annotated_image_b64", lambda uid: "c2hvdWxkLW5vdA=="
    )

    result = app_module.run_agent([HumanMessage(content="detect, rotate, then show annotated")])

    # The prediction was reset by rotate, so no annotated image is surfaced.
    assert result["prediction_id"] is None
    assert result["annotated_image"] is None


def _make_png_bytes(width, height, mode="RGB"):
    import io

    from PIL import Image

    image = Image.new(mode, (width, height), color=(255, 0, 0) if mode == "RGB" else (255, 0, 0, 128))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_resize_for_display_downscales_large_image():
    import io

    from PIL import Image

    # A 2400x1200 image exceeds the 1200px longest-side limit.
    data = _make_png_bytes(2400, 1200)

    out = app_module._resize_for_display(data)

    with Image.open(io.BytesIO(out)) as image:
        # Longest side is clamped to 1200, aspect ratio preserved (2:1 -> 1200x600).
        assert max(image.size) == 1200
        assert image.size == (1200, 600)


def test_resize_for_display_keeps_small_image_unchanged():
    # A 800x600 image is under the limit and must be returned byte-for-byte.
    data = _make_png_bytes(800, 600)

    assert app_module._resize_for_display(data) == data


def test_resize_for_display_uses_jpeg_for_opaque_image():
    import io

    from PIL import Image

    data = _make_png_bytes(2000, 1000, mode="RGB")

    out = app_module._resize_for_display(data)

    with Image.open(io.BytesIO(out)) as image:
        assert image.format == "JPEG"


def test_resize_for_display_preserves_png_for_transparent_image():
    import io

    from PIL import Image

    data = _make_png_bytes(2000, 1000, mode="RGBA")

    out = app_module._resize_for_display(data)

    with Image.open(io.BytesIO(out)) as image:
        # Transparency must be preserved, so PNG is used, not JPEG.
        assert image.format == "PNG"
        assert image.mode == "RGBA"


def test_resize_for_display_returns_original_on_invalid_image():
    # Non-image bytes must not raise; the input is returned unchanged so display
    # still works.
    data = b"not-an-image"

    assert app_module._resize_for_display(data) == data



