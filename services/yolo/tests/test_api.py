import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.5")

from app import app, init_db

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_predictions.db")
    monkeypatch.setattr("app.DB_PATH", db_file)
    init_db()


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_predict_response_schema(client):
    with open(TEST_IMAGE, "rb") as f:
        response = client.post(
            "/predict",
            files={"file": ("beatles.jpeg", f, "image/jpeg")},
        )

    assert response.status_code == 200
    data = response.json()

    assert set(data.keys()) == {
        "prediction_uid",
        "detection_count",
        "labels",
        "time_took",
    }
    assert isinstance(data["prediction_uid"], str)
    assert isinstance(data["detection_count"], int)
    assert isinstance(data["labels"], list)
    assert all(isinstance(label, str) for label in data["labels"])
    assert isinstance(data["time_took"], (int, float))
    assert data["detection_count"] == len(data["labels"])



