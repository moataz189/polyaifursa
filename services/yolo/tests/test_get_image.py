import os
import shutil
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app


ORIGINAL_UPLOAD_DIR = app_module.UPLOAD_DIR
ORIGINAL_PREDICTED_DIR = app_module.PREDICTED_DIR


def setup_dirs():
    original_dir = tempfile.mkdtemp()
    predicted_dir = tempfile.mkdtemp()

    app_module.UPLOAD_DIR = original_dir
    app_module.PREDICTED_DIR = predicted_dir

    client = TestClient(app)

    return client, original_dir, predicted_dir


def teardown_dirs(original_dir, predicted_dir):
    app_module.UPLOAD_DIR = ORIGINAL_UPLOAD_DIR
    app_module.PREDICTED_DIR = ORIGINAL_PREDICTED_DIR

    shutil.rmtree(original_dir)
    shutil.rmtree(predicted_dir)


def test_get_original_image_success():
    client, original_dir, predicted_dir = setup_dirs()

    try:
        image_path = os.path.join(original_dir, "test.jpg")

        with open(image_path, "wb") as f:
            f.write(b"fake image content")

        response = client.get("/image/original/test.jpg")

        assert response.status_code == 200

    finally:
        teardown_dirs(original_dir, predicted_dir)


def test_get_image_invalid_type():
    client, original_dir, predicted_dir = setup_dirs()

    try:
        response = client.get("/image/banana/test.jpg")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid image type"

    finally:
        teardown_dirs(original_dir, predicted_dir)


def test_get_image_not_found():
    client, original_dir, predicted_dir = setup_dirs()

    try:
        response = client.get("/image/original/not_exists.jpg")

        assert response.status_code == 404
        assert response.json()["detail"] == "Image not found"

    finally:
        teardown_dirs(original_dir, predicted_dir)