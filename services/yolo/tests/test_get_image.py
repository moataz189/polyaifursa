import os
import shutil
import tempfile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app as app_module
from app import app
from db import get_db
from models import Base

ORIGINAL_UPLOAD_DIR = app_module.UPLOAD_DIR
ORIGINAL_PREDICTED_DIR = app_module.PREDICTED_DIR

##utility functions to setup and teardown temporary directories for testing
def setup_dirs():
    original_dir = tempfile.mkdtemp()
    predicted_dir = tempfile.mkdtemp()

    app_module.UPLOAD_DIR = original_dir
    app_module.PREDICTED_DIR = predicted_dir

    # Setup database dependency (though not used in this test file)
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

    client = TestClient(app)

    return client, original_dir, predicted_dir


def teardown_dirs(original_dir, predicted_dir):
    app_module.UPLOAD_DIR = ORIGINAL_UPLOAD_DIR
    app_module.PREDICTED_DIR = ORIGINAL_PREDICTED_DIR
    app.dependency_overrides.clear()

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
    
def test_get_predicted_image_success():
    client, original_dir, predicted_dir = setup_dirs()

    try:
            image_path = os.path.join(predicted_dir, "test.jpg")
            with open(image_path, "wb") as f:
                f.write(b"fake image content")

            response = client.get("/image/predicted/test.jpg")
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