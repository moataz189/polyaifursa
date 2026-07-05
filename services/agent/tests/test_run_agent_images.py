"""Image state: processed_image, annotated_image, prediction/image-id updates,
and display-resizing of returned images."""

import json

from langchain_core.messages import HumanMessage, ToolMessage

import app as app_module
from tests.conftest import (
    AsyncProcTool,
    FakeLLM,
    FakeTool,
    _ai_final,
    _ai_tool_call,
    _make_png_bytes,
    _register_proc,
)


def test_run_agent_returns_processed_image(monkeypatch):
    app_module._latest_prediction_uid.set(None)

    # Script: rotate (image-processing tool) -> final answer.
    responses = [
        _ai_tool_call("rotate", "call_1"),
        _ai_final("Rotated your image."),
    ]
    monkeypatch.setattr(app_module, "llm_with_tools", FakeLLM(responses))

    output_key = "chat/pred/processed/rotate_90_test.png"
    _register_proc(monkeypatch, {"rotate": AsyncProcTool(output_key)})

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

    _register_proc(monkeypatch, {"detect_objects": RecordingTool()})

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

    rotate_tool = AsyncProcTool("chat/pred/processed/rotate_test.png")
    blur_tool = AsyncProcTool("chat/pred/processed/blur_test.png")
    _register_proc(monkeypatch, {"rotate": rotate_tool, "blur": blur_tool})
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")

    result = app_module.run_agent([HumanMessage(content="rotate then blur")])

    # rotate saw the original; blur saw rotate's output.
    assert rotate_tool.seen_input_keys == ["chat/pred/original/pic.png"]
    assert blur_tool.seen_input_keys == ["chat/pred/processed/rotate_test.png"]
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

    _register_proc(
        monkeypatch,
        {
            "detect_objects": FakeTool({"prediction_uid": "predA"}),
            "blur": AsyncProcTool("chat/pred/processed/blur_B.png"),
        },
    )
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

    _register_proc(
        monkeypatch,
        {
            "detect_objects": FakeTool({"prediction_uid": "predA"}),
            "blur": AsyncProcTool("chat/pred/processed/blur_B.png"),
            "show_annotated_image": FakeTool(
                {"image_url": "http://yolo.example/prediction/predA/image"}
            ),
        },
    )
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

    _register_proc(
        monkeypatch,
        {
            "add_noise": AsyncProcTool(noisy_key),
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
    _register_proc(monkeypatch, {"rotate": AsyncProcTool(output_key)})
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
    _register_proc(
        monkeypatch,
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
    _register_proc(monkeypatch, fake_tools)
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

    _register_proc(
        monkeypatch,
        {
            "detect_objects": FakeTool({"prediction_uid": "uid-1"}),
            "rotate": AsyncProcTool("chat/img-1/processed/rotate_90_pic.png"),
            "show_annotated_image": FakeTool(
                {"image_url": "http://yolo.example/prediction/uid-1/image"}
            ),
        },
    )
    monkeypatch.setattr(app_module, "download_image", lambda key: b"bytes")
    monkeypatch.setattr(
        app_module, "_fetch_annotated_image_b64", lambda uid: "c2hvdWxkLW5vdA=="
    )

    result = app_module.run_agent([HumanMessage(content="detect, rotate, then show annotated")])

    # The prediction was reset by rotate, so no annotated image is surfaced.
    assert result["prediction_id"] is None
    assert result["annotated_image"] is None


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
