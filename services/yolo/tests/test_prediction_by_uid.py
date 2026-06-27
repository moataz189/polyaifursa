import tempfile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import app
from db import get_db
from models import Base, PredictionSession, DetectionObject


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
    return TestClient(app), TestSessionLocal


def test_get_prediction_by_uid_success():
    client, session_local = setup_db()

    with session_local() as db:
        db.add(PredictionSession(
            uid="abc-123",
            original_image="original.jpg",
            predicted_image="predicted.jpg",
        ))
        db.flush()
        db.add(DetectionObject(
            prediction_uid="abc-123",
            label="person",
            score=0.91,
            box="[10, 20, 100, 200]"
        ))
        db.commit()

    response = client.get("/prediction/abc-123")

    assert response.status_code == 200

    data = response.json()
    assert data["uid"] == "abc-123"
    assert "timestamp" in data
    assert data["original_image"] == "original.jpg"
    assert data["predicted_image"] == "predicted.jpg"

    assert len(data["detection_objects"]) == 1
    assert data["detection_objects"][0]["label"] == "person"
    assert data["detection_objects"][0]["score"] == 0.91
    assert data["detection_objects"][0]["box"] == "[10, 20, 100, 200]"


def test_get_prediction_by_uid_not_found():
    client, _ = setup_db()

    response = client.get("/prediction/not-found")

    assert response.status_code == 404
    assert response.json()["detail"] == "Prediction not found"
    