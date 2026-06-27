import tempfile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import app
from db import get_db
from models import Base, DetectionObject


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


def test_get_predictions_by_score_found():
    client, session_local = setup_db()

    with session_local() as db:
        db.add(DetectionObject(
            prediction_uid="abc-123",
            label="person",
            score=0.91,
            box="[10, 20, 100, 200]"
        ))
        db.commit()

    response = client.get("/predictions/score/0.5")

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1
    assert data[0]["prediction_uid"] == "abc-123"
    assert data[0]["label"] == "person"
    assert data[0]["score"] == 0.91


def test_get_predictions_by_score_no_matches():
    client, session_local = setup_db()

    with session_local() as db:
        db.add(DetectionObject(
            prediction_uid="abc-123",
            label="person",
            score=0.20,
            box="[10, 20, 100, 200]"
        ))
        db.commit()

    response = client.get("/predictions/score/0.5")

    assert response.status_code == 200
    assert response.json() == []


def test_get_predictions_by_score_invalid_score():
    client, _ = setup_db()

    response = client.get("/predictions/score/1.5")

    assert response.status_code == 400
    assert response.json()["detail"] == \
        "min_score must be between 0.0 and 1.0"
    