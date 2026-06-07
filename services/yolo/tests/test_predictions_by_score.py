import sqlite3
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


def setup_db():
    _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
    init_db()
    return TestClient(app)


def test_get_predictions_by_score_found():
    client = setup_db()

    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO detection_objects
            (prediction_uid, label, score, box)
            VALUES (?, ?, ?, ?)
        """, ("abc-123", "person", 0.91, "[10, 20, 100, 200]"))

    response = client.get("/predictions/score/0.5")

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1
    assert data[0]["prediction_uid"] == "abc-123"
    assert data[0]["label"] == "person"
    assert data[0]["score"] == 0.91


def test_get_predictions_by_score_no_matches():
    client = setup_db()

    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO detection_objects
            (prediction_uid, label, score, box)
            VALUES (?, ?, ?, ?)
        """, ("abc-123", "person", 0.20, "[10, 20, 100, 200]"))

    response = client.get("/predictions/score/0.5")

    assert response.status_code == 200
    assert response.json() == []


def test_get_predictions_by_score_invalid_score():
    client = setup_db()
#   check for score less than 0.0
    response = client.get("/predictions/score/1.5")

    assert response.status_code == 400
    assert response.json()["detail"] == \
        "min_score must be between 0.0 and 1.0"
    