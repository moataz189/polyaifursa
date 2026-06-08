from contextlib import closing
import sqlite3
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


def setup_db():
    _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
    init_db()
    return TestClient(app)


def test_get_predictions_by_label_found():
    client = setup_db()

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
        """, ("abc-123", "person", 0.91, "[10,20,100,200]"))
        conn.commit()
    response = client.get("/predictions/label/person")

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1
    assert data[0]["uid"] == "abc-123"
    assert data[0]["detection_objects"][0]["label"] == "person"


def test_get_predictions_by_label_no_matches():
    client = setup_db()

    response = client.get("/predictions/label/elephant")

    assert response.status_code == 200
    assert response.json() == []


def test_get_predictions_by_empty_label():
    client = setup_db()


    response = client.get("/predictions/label/%20")

    assert response.status_code == 400
    assert response.json()["detail"] == "Label cannot be empty"
    #add note to check for empty label with spaces only
   