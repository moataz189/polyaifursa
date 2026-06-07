import sqlite3
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


def setup_db():
    _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
    init_db()
    return TestClient(app)

def test_get_prediction_image_success(tmp_path):
    client = setup_db()

    image_path = tmp_path / "predicted.jpg"
    image_path.write_bytes(b"fake image content")

    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO prediction_sessions
            (uid, original_image, predicted_image)
            VALUES (?, ?, ?)
        """, ("abc-123", "original.jpg", str(image_path)))

    response = client.get("/prediction/abc-123/image")

    assert response.status_code == 200


def test_get_prediction_image_not_found(tmp_path):
    client = setup_db()

    response = client.get("/prediction/not-found/image")

    assert response.status_code == 404
    assert response.json()["detail"] == "Image not found"