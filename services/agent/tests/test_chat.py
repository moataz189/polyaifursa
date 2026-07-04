import base64
import os
import time

os.environ.setdefault("MODEL", "openai.gpt-oss-20b-1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")

import pytest
from fastapi.testclient import TestClient
from langchain_core.rate_limiters import InMemoryRateLimiter

import app as app_module
from app import app
from s3 import safe_image_name


@pytest.fixture
def client():
    return TestClient(app)


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


def test_chat_response_schema(client, monkeypatch):
    # Avoid real LLM/YOLO calls by mocking the agent loop.
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "What is in this image?"}],
        },
    )

    assert response.status_code == 200
    data = response.json()

    for field in [
        "response",
        "prediction_id",
        "annotated_image",
        "agent_loop_time_s",
        "iterations",
        "tools_called",
        "context_limit_exceeded",
        "tokens_used",
    ]:
        assert field in data

    assert data["response"] == "I found a person and a guitar."
    assert data["prediction_id"] == "abc-123"
    assert data["annotated_image"] == "aGVsbG8="
    assert isinstance(data["agent_loop_time_s"], (int, float))
    assert data["iterations"] == 2
    assert data["tools_called"] == ["detect_objects", "show_annotated_image"]
    assert data["context_limit_exceeded"] is False
    # Backward-compatible field is preserved.
    assert data["image_url"] == "http://yolo.example/prediction/abc-123/image"
    # New processed-image field is present (null when no processing tool ran).
    assert "processed_image" in data
    assert data["processed_image"] is None


def test_chat_returns_processed_image(client, monkeypatch):
    result = _fake_agent_result()
    result["processed_image"] = "cHJvY2Vzc2Vk"
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: result)

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "rotate the image 90 degrees"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["processed_image"] == "cHJvY2Vzc2Vk"


def test_chat_tokens_used(client, monkeypatch):
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "What is in this image?"}],
        },
    )

    assert response.status_code == 200
    data = response.json()

    assert "tokens_used" in data
    tokens_used = data["tokens_used"]
    for field in ["input", "output", "total"]:
        assert field in tokens_used
        assert isinstance(tokens_used[field], int)

    assert tokens_used["input"] == 312
    assert tokens_used["output"] == 22
    assert tokens_used["total"] == 334


def test_chat_uploads_original_image_to_s3(client, monkeypatch):
    # An image in the request must be uploaded to S3, and only its key is kept;
    # the raw bytes never flow into the agent loop.
    uploaded = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        uploaded["key"] = key
        uploaded["data"] = data
        return key

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    # 1x1 PNG pixel, base64 encoded.
    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-xyz",
            "messages": [
                {
                    "role": "user",
                    "content": "What is in this image?",
                    "image_base64": image_b64,
                    "image_filename": "my_photo.png",
                }
            ],
        },
    )

    assert response.status_code == 200
    # Key follows <chat_id>/<prediction_id>/original/<image_name> and preserves
    # the original uploaded filename (never a hard-coded "image.jpg").
    assert "key" in uploaded
    parts = uploaded["key"].split("/")
    assert len(parts) == 4
    # The client-supplied chat_id is used, not a server-generated one.
    assert parts[0] == "chat-xyz"
    assert parts[2] == "original"
    assert parts[3] == "my_photo.png"
    # The uploaded bytes match the decoded image.
    assert uploaded["data"] == base64.b64decode(image_b64)


def test_chat_uploads_image_without_filename_uses_fallback(client, monkeypatch):
    # When no filename is supplied, a generated name is used (no hard-coded
    # "image.jpg" and no crash).
    uploaded = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        uploaded["key"] = key
        return key

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "What is in this image?",
                    "image_base64": image_b64,
                }
            ],
        },
    )

    assert response.status_code == 200
    parts = uploaded["key"].split("/")
    assert parts[0] == "chat-abc"
    assert parts[2] == "original"
    # No filename -> fallback "<prediction_id>.jpg" (the prediction id is the
    # key's second segment). Never the old hard-coded "image.jpg".
    prediction_id = parts[1]
    assert parts[3] == f"{prediction_id}.jpg"
    assert parts[3] != "image.jpg"


def test_chat_uploads_only_newest_image(client, monkeypatch):
    # The conversation history carries an old image, but the latest user
    # message has a new one. Only the newest image must be uploaded.
    uploads = []

    def fake_upload(key, data, content_type="image/jpeg"):
        uploads.append(key)
        return key

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "old image",
                    "image_base64": image_b64,
                    "image_filename": "old.png",
                },
                {"role": "assistant", "content": "I saw the old image."},
                {
                    "role": "user",
                    "content": "new image",
                    "image_base64": image_b64,
                    "image_filename": "new.png",
                },
            ],
        },
    )

    assert response.status_code == 200
    # Exactly one upload, and it is the newest image (old.png is NOT re-uploaded).
    assert len(uploads) == 1
    assert uploads[0].endswith("/original/new.png")
    assert uploads[0].startswith("chat-abc/")


def test_chat_text_only_follow_up_uploads_nothing(client, monkeypatch):
    # A text-only latest message must not trigger any upload, even if an earlier
    # message had an image.
    uploads = []
    monkeypatch.setattr(
        app_module, "upload_image", lambda key, data, content_type="image/jpeg": uploads.append(key)
    )
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "here is an image",
                    "image_base64": image_b64,
                    "image_filename": "pic.png",
                },
                {"role": "assistant", "content": "Got it."},
                {"role": "user", "content": "tell me more"},
            ],
        },
    )

    assert response.status_code == 200
    assert uploads == []


def test_chat_returns_new_image_key(client, monkeypatch):
    # When a new image is uploaded, its S3 key is echoed back in
    # latest_image_s3_key so the client can reuse it on follow-up requests.
    captured = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        captured["key"] = key
        return key

    def fake_run_agent(*args, **kwargs):
        # The real run_agent seeds latest_image_s3_key from the context var.
        result = _fake_agent_result()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "here is an image",
                    "image_base64": image_b64,
                    "image_filename": "pic.png",
                }
            ],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["latest_image_s3_key"] == captured["key"]


def test_chat_reuses_previous_image_key(client, monkeypatch):
    # A follow-up request without a new image reuses the client-supplied
    # latest_image_s3_key, sets it as the current image, and echoes it back.
    seen_key = {}

    def fake_run_agent(*args, **kwargs):
        seen_key["value"] = app_module._current_image_s3_key.get()
        result = _fake_agent_result()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    # No new image means no upload should ever happen.
    monkeypatch.setattr(
        app_module,
        "upload_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    prior_key = "chat-abc/pred-1/original/pic.png"

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "rotate the previous image 90 degrees"}],
            "latest_image_s3_key": prior_key,
        },
    )

    assert response.status_code == 200
    data = response.json()
    # The tools saw the carried-over key, and it is echoed back to the client.
    assert seen_key["value"] == prior_key
    assert data["latest_image_s3_key"] == prior_key


def test_chat_new_image_takes_precedence_over_previous_key(client, monkeypatch):
    # If both a new upload and a previous key are present, the new upload wins.
    captured = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        captured["key"] = key
        return key

    seen_key = {}

    def fake_run_agent(*args, **kwargs):
        seen_key["value"] = app_module._current_image_s3_key.get()
        result = _fake_agent_result()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "here is a new image",
                    "image_base64": image_b64,
                    "image_filename": "new.png",
                }
            ],
            "latest_image_s3_key": "chat-abc/old/original/old.png",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert seen_key["value"] == captured["key"]
    assert data["latest_image_s3_key"] == captured["key"]
    assert data["latest_image_s3_key"] != "chat-abc/old/original/old.png"


def test_chat_no_image_and_no_previous_key(client, monkeypatch):
    # Pure text conversation with no image anywhere: latest_image_s3_key is null.
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["latest_image_s3_key"] is None


def test_chat_new_image_resets_previous_prediction_id(client, monkeypatch):
    # Uploading a new image must drop any carried-over prediction id (it belongs
    # to an OLDER image). The agent should be seeded with prediction_id = None.
    seen = {}

    def fake_run_agent(*args, **kwargs):
        seen["prediction"] = app_module._latest_prediction_uid.get()
        seen["image_key"] = app_module._current_image_s3_key.get()
        return _fake_agent_result()

    monkeypatch.setattr(
        app_module, "upload_image", lambda key, data, content_type="image/jpeg": key
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "here is a new image",
                    "image_base64": image_b64,
                    "image_filename": "new.png",
                }
            ],
            # Stale state from an older image that must be discarded.
            "latest_prediction_id": "old-pred",
            "latest_image_s3_key": "chat-abc/old/original/old.png",
        },
    )

    assert response.status_code == 200
    # The old prediction was dropped; the tools see no detection for the new image.
    assert seen["prediction"] is None
    # The new upload is the image in play, not the old key.
    assert seen["image_key"].endswith("/original/new.png")


def test_chat_new_image_creates_image_id_and_original_key(client, monkeypatch):
    # Uploading a new image must generate an image_id, store the original under
    # <chat_id>/<image_id>/original/<filename>, seed the tools with that key and
    # image_id, and reset any prior prediction.
    seen = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        seen["key"] = key
        return key

    def fake_run_agent(*args, **kwargs):
        seen["image_id"] = app_module._current_image_id.get()
        seen["image_key"] = app_module._current_image_s3_key.get()
        seen["original_key"] = app_module._original_image_s3_key.get()
        result = _fake_agent_result()
        # The real run_agent echoes the current image_id back.
        result["latest_image_id"] = app_module._current_image_id.get()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-xyz",
            "messages": [
                {
                    "role": "user",
                    "content": "here is an image",
                    "image_base64": image_b64,
                    "image_filename": "cat.png",
                }
            ],
        },
    )

    assert response.status_code == 200
    data = response.json()

    # The original image key follows <chat_id>/<image_id>/original/<filename>.
    parts = seen["key"].split("/")
    assert parts[0] == "chat-xyz"
    assert parts[2] == "original"
    assert parts[3] == "cat.png"
    image_id = parts[1]

    # The context is seeded with the same image_id and original key.
    assert seen["image_id"] == image_id
    assert seen["image_key"] == seen["key"]
    assert seen["original_key"] == seen["key"]

    # The image_id round-trips to the client and is distinct from a prediction.
    assert data["latest_image_id"] == image_id
    assert data["latest_image_s3_key"] == seen["key"]


def test_chat_reuses_previous_image_id(client, monkeypatch):
    # A follow-up request without a new image carries over latest_image_id and
    # seeds the tools with it (same image flow), echoing it back.
    seen = {}

    def fake_run_agent(*args, **kwargs):
        seen["image_id"] = app_module._current_image_id.get()
        result = _fake_agent_result()
        result["latest_image_id"] = app_module._current_image_id.get()
        return result

    monkeypatch.setattr(
        app_module,
        "upload_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "rotate the previous image"}],
            "latest_image_s3_key": "chat-abc/img-7/original/pic.png",
            "latest_image_id": "img-7",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert seen["image_id"] == "img-7"
    assert data["latest_image_id"] == "img-7"


def test_chat_seeds_original_key_from_request_not_latest(client, monkeypatch):
    # A follow-up request after processing: latest_image_s3_key points at the
    # processed image, but original_image_s3_key must be seeded from the
    # client-supplied original key, NOT derived from latest_image_s3_key.
    seen = {}

    def fake_run_agent(*args, **kwargs):
        seen["original"] = app_module._original_image_s3_key.get()
        seen["latest"] = app_module._current_image_s3_key.get()
        result = _fake_agent_result()
        result["original_image_s3_key"] = app_module._original_image_s3_key.get()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(
        app_module,
        "upload_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    processed_key = "chat-abc/img-7/processed/add_noise_pic.png"
    original_key = "chat-abc/img-7/original/pic.png"

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "detect the original image"}],
            "latest_image_s3_key": processed_key,
            "latest_image_id": "img-7",
            "original_image_s3_key": original_key,
        },
    )

    assert response.status_code == 200
    data = response.json()
    # The original key comes from the request, not the processed latest key.
    assert seen["original"] == original_key
    assert seen["latest"] == processed_key
    assert data["original_image_s3_key"] == original_key


def test_chat_context_limit_exceeded(client, monkeypatch):
    result = _fake_agent_result()
    result["context_limit_exceeded"] = True
    result["response"] = "Agent stopped: maximum iterations reached."
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: result)

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "Loop forever"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["context_limit_exceeded"] is True


def test_llm_has_in_memory_rate_limiter():
    # The chat model must be initialized with a rate limiter, and it must be
    # an InMemoryRateLimiter configured for a single-user dev deployment.
    rate_limiter = app_module.llm.rate_limiter

    assert rate_limiter is not None
    assert isinstance(rate_limiter, InMemoryRateLimiter)
    assert rate_limiter.requests_per_second == 1
    assert rate_limiter.max_bucket_size == 5


def test_rate_limiter_delays_when_bucket_exhausted():
    # Behavioral test: a dedicated limiter (NOT the app's global one) with a
    # bucket size of 1 and 1 token/sec. The first acquire drains the bucket;
    # the second must wait ~1s for a token to refill.
    limiter = InMemoryRateLimiter(
        requests_per_second=1,
        check_every_n_seconds=0.01,
        max_bucket_size=1,
    )

    # Consume the only available token immediately (should not block).
    assert limiter.acquire(blocking=True) is True

    # The bucket is now empty; the next token costs ~1 second to refill.
    start = time.perf_counter()
    assert limiter.acquire(blocking=True) is True
    elapsed = time.perf_counter() - start

    # Allow generous tolerance so the test is not flaky on slow/busy machines:
    # it must clearly wait (not instant) but stay in a reasonable upper bound.
    assert 0.8 <= elapsed <= 2.0


def test_safe_image_name_preserves_filename_and_extension():
    assert safe_image_name("photo.png") == "photo.png"
    assert safe_image_name("beatles.JPEG") == "beatles.JPEG"
    # Directory components are stripped so the name stays a single key segment.
    assert safe_image_name("../../etc/passwd") == "passwd"
    assert safe_image_name("/abs/path/cat.jpg") == "cat.jpg"


def test_safe_image_name_falls_back_when_missing():
    # No usable filename -> generated name, never the hard-coded "image.jpg".
    for value in [None, "", "   ", ".", ".."]:
        name = safe_image_name(value)
        assert name
        assert name != "image.jpg"
        assert "/" not in name

