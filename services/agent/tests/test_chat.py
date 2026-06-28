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
        "prediction_id": "abc-123",
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

