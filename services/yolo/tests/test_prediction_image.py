import tempfile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import app
from db import get_db
from models import Base, PredictionSession


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


def test_get_prediction_image_success(tmp_path):
    client, session_local = setup_db()

    image_path = tmp_path / "predicted.jpg"
    image_path.write_bytes(b"fake image content")

    with session_local() as db:
        db.add(PredictionSession(
            uid="abc-123",
            original_image="original.jpg",
            predicted_image=str(image_path)
        ))
        db.commit()

    response = client.get("/prediction/abc-123/image")

    assert response.status_code == 200


def test_get_prediction_image_not_found():
    client, _ = setup_db()

    response = client.get("/prediction/not-found/image")

    assert response.status_code == 404
    assert response.json()["detail"] == "Image not found"