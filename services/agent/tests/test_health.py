import os

# Configure the environment BEFORE importing the app module, because app.py
# validates MODEL and builds the LLM client at import time.
os.environ.setdefault("MODEL", "openai:gpt-5.4-mini")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
