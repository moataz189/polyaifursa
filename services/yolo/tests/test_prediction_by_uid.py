from contextlib import closing
import sqlite3
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


def setup_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_predictions.db")
    monkeypatch.setattr("app.DB_PATH", db_file)
    init_db()
    return TestClient(app)


def test_get_prediction_by_uid_success(tmp_path, monkeypatch):
    client = setup_db(tmp_path, monkeypatch)

    with closing(sqlite3.connect(app_module.DB_PATH)) as conn:
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
        conn.commit()
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


def test_get_prediction_by_uid_not_found(tmp_path, monkeypatch):
    client = setup_db(tmp_path, monkeypatch)

    response = client.get("/prediction/not-found")

    assert response.status_code == 404
    assert response.json()["detail"] == "Prediction not found"
    