import tempfile
from datetime import datetime, timedelta, timezone
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


def test_get_recent_predictions_empty():
    """Test GET /predictions/recent with no predictions"""
    client, _ = setup_db()

    response = client.get("/predictions/recent")

    assert response.status_code == 200
    assert response.json() == []


def test_get_recent_predictions_single():
    """Test GET /predictions/recent with one prediction"""
    client, session_local = setup_db()

    with session_local() as db:
        db.add(PredictionSession(
            uid="pred-1",
            original_image="original.jpg",
            predicted_image="predicted.jpg",
        ))
        db.flush()
        db.add(DetectionObject(
            prediction_uid="pred-1",
            label="person",
            score=0.95,
            box="[10, 20, 100, 200]"
        ))
        db.commit()

    response = client.get("/predictions/recent")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["uid"] == "pred-1"
    assert data[0]["original_image"] == "original.jpg"
    assert data[0]["predicted_image"] == "predicted.jpg"
    assert len(data[0]["detection_objects"]) == 1
    assert data[0]["detection_objects"][0]["label"] == "person"
    assert data[0]["detection_objects"][0]["score"] == 0.95


def test_get_recent_predictions_limit_10():
    """Test GET /predictions/recent returns max 10 results"""
    client, session_local = setup_db()

    # Create 15 prediction sessions
    with session_local() as db:
        for i in range(15):
            session_obj = PredictionSession(
                uid=f"pred-{i}",
                original_image=f"original-{i}.jpg",
                predicted_image=f"predicted-{i}.jpg",
            )
            db.add(session_obj)
        db.commit()

    response = client.get("/predictions/recent")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 10  # Should be limited to 10


def test_get_recent_predictions_ordering():
    """Test GET /predictions/recent returns newest first"""
    client, session_local = setup_db()

    # Create 3 predictions with explicit timestamps for ordering
    with session_local() as db:
        now = datetime.now(timezone.utc)
        for i in range(3):
            session_obj = PredictionSession(
                uid=f"pred-{i}",
                original_image=f"original-{i}.jpg",
                predicted_image=f"predicted-{i}.jpg",
                timestamp=now - timedelta(hours=(2-i))  # pred-0 oldest, pred-2 newest
            )
            db.add(session_obj)
        db.commit()

    response = client.get("/predictions/recent")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    # Verify they are ordered by timestamp descending (newest first)
    assert data[0]["uid"] == "pred-2"
    assert data[1]["uid"] == "pred-1"
    assert data[2]["uid"] == "pred-0"


def test_get_recent_predictions_with_multiple_detections():
    """Test GET /predictions/recent includes all detection objects"""
    client, session_local = setup_db()

    with session_local() as db:
        session_obj = PredictionSession(
            uid="pred-1",
            original_image="original.jpg",
            predicted_image="predicted.jpg",
        )
        db.add(session_obj)
        db.flush()
        
        # Add multiple detection objects
        for i in range(3):
            db.add(DetectionObject(
                prediction_uid="pred-1",
                label=f"object-{i}",
                score=0.8 + (i * 0.05),
                box=f"[{i*10}, {i*10}, {i*10+50}, {i*10+50}]"
            ))
        db.commit()

    response = client.get("/predictions/recent")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert len(data[0]["detection_objects"]) == 3
    assert data[0]["detection_objects"][0]["label"] == "object-0"
    assert data[0]["detection_objects"][1]["label"] == "object-1"
    assert data[0]["detection_objects"][2]["label"] == "object-2"
