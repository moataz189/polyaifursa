import os
import pytest
import tempfile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.5")

from app import app
from db import get_db
from models import Base

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


def setup_db():
    _, db_path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_s3(monkeypatch):
    """Mock boto3 access so the predict flow never touches real AWS.

    download returns the local test image bytes; upload is a no-op that just
    records the keys it was asked to store.
    """
    with open(TEST_IMAGE, "rb") as f:
        image_bytes = f.read()

    uploaded = {}

    monkeypatch.setattr("app.download_image", lambda key: image_bytes)
    monkeypatch.setattr(
        "app.upload_image",
        lambda key, data, content_type="image/jpeg": uploaded.setdefault(key, data) or key,
    )
    return uploaded


@pytest.fixture
def client():
    return setup_db()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}



def test_check(client):
    response = client.get("/check")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_shutting_down(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "is_shutting_down", True)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "Service is shutting down"


def test_predict_response_schema(client, mock_s3):
    response = client.post(
        "/predict",
        json={"image_s3_key": "chat-1/pred-1/original/beatles.jpeg"},
    )

    assert response.status_code == 200
    data = response.json()

    assert set(data.keys()) == {
        "prediction_uid",
        "detection_count",
        "labels",
        "time_took",
        "predicted_image_s3_key",
    }

    assert isinstance(data["prediction_uid"], str)
    assert isinstance(data["detection_count"], int)
    assert isinstance(data["labels"], list)
    assert all(isinstance(label, str) for label in data["labels"])
    assert isinstance(data["time_took"], (int, float))
    assert data["detection_count"] == len(data["labels"])

    # The prediction id is a fresh UUID (NOT derived from the S3 key). The
    # annotated image is stored under the per-prediction folder:
    #   <chat_id>/<image_id>/predictions/<prediction_uid>/annotated_<name>.png
    import uuid as _uuid

    uid = data["prediction_uid"]
    assert _uuid.UUID(uid)  # valid UUID, raises otherwise

    expected_predicted_key = f"chat-1/pred-1/predictions/{uid}/annotated_beatles.png"
    assert data["predicted_image_s3_key"] == expected_predicted_key
    assert expected_predicted_key in mock_s3


def test_predict_annotated_key_layout(client, mock_s3):
    # The annotated image must be stored under the image_id's predictions folder,
    # keyed by the fresh prediction_uid, so it is separated from the image flow.
    response = client.post(
        "/predict",
        json={"image_s3_key": "chat-9/img-42/original/beatles.jpeg"},
    )

    assert response.status_code == 200
    data = response.json()
    uid = data["prediction_uid"]

    assert data["predicted_image_s3_key"] == (
        f"chat-9/img-42/predictions/{uid}/annotated_beatles.png"
    )
    # image_id (img-42) and prediction_uid must be different values.
    assert uid != "img-42"


def test_predict_same_key_creates_unique_uids(client, mock_s3):
    # Detecting the SAME image key twice must succeed and produce two DIFFERENT
    # prediction uids (no prediction_sessions.uid UNIQUE constraint failure).
    payload = {"image_s3_key": "chat-1/pred-1/original/beatles.jpeg"}

    first = client.post("/predict", json=payload)
    second = client.post("/predict", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200

    uid_1 = first.json()["prediction_uid"]
    uid_2 = second.json()["prediction_uid"]

    assert uid_1 != uid_2

    # Both sessions are retrievable under their own uid.
    assert client.get(f"/prediction/{uid_1}").status_code == 200
    assert client.get(f"/prediction/{uid_2}").status_code == 200
