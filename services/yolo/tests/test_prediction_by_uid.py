import sqlite3
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


def setup_db():
    _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
    init_db()
    return TestClient(app)


def test_get_prediction_by_uid_success():
    client = setup_db()
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO prediction_sessions
            (uid, original_image, predicted_image)
            VALUES (?, ?, ?)
        """, ("abc-123", "original.jpg", "predicted.jpg"))

        conn.execute("""
            INSERT INTO detection_objects
            (prediction_uid, label, score, box)
            VALUES (?, ?, ?, ?)
        """, ("abc-123", "person", 0.91, "[10, 20, 100, 200]"))

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
    client = setup_db()

    response = client.get("/prediction/not-found")

    assert response.status_code == 404
    assert response.json()["detail"] == "Prediction not found"
    