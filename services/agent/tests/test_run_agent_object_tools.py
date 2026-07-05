"""Object tools: detect_objects source resolution and select_object bbox flow."""

import json

import pytest

import app as app_module
from tests.conftest import (
    _FakeGetClient,
    _FakeHTTPClient,
    _SAMPLE_DETECTIONS,
)


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


# --- Object-specific selection --------------------------------------------


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
