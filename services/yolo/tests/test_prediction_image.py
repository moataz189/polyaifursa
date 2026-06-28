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

def test_get_prediction_image_success(monkeypatch):
    client = setup_db()

    # The predicted image now lives in S3; mock the download.
    monkeypatch.setattr(app_module, "download_image", lambda key: b"fake image content")

    with closing(sqlite3.connect(app_module.DB_PATH)) as conn:
        conn.execute("""
            INSERT INTO prediction_sessions
            (uid, original_image, predicted_image)
            VALUES (?, ?, ?)
        """, ("abc-123", "chat-1/abc-123/original/img.jpg", "chat-1/abc-123/predicted/img.jpg"))
        conn.commit()

    response = client.get("/prediction/abc-123/image")

    assert response.status_code == 200
    assert response.content == b"fake image content"


def test_get_prediction_image_not_found():
    client = setup_db()

    response = client.get("/prediction/not-found/image")

    assert response.status_code == 404
    assert response.json()["detail"] == "Image not found"