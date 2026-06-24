import os
import time

# Configure the environment BEFORE importing the app module, because app.py
# validates MODEL and builds the LLM client at import time.
os.environ.setdefault("MODEL", "openai:gpt-5.4-mini")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient
from langchain_core.rate_limiters import InMemoryRateLimiter

import app as app_module
from app import app


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
        json={"messages": [{"role": "user", "content": "What is in this image?"}]},
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
        json={"messages": [{"role": "user", "content": "What is in this image?"}]},
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


def test_chat_context_limit_exceeded(client, monkeypatch):
    result = _fake_agent_result()
    result["context_limit_exceeded"] = True
    result["response"] = "Agent stopped: maximum iterations reached."
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: result)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "Loop forever"}]},
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
