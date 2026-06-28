import os
import unittest
import tempfile
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import app
from db import get_db
from models import Base

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


class TestPredictionTime(unittest.TestCase):
    def setUp(self):
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
        self.client = TestClient(app)

        with open(TEST_IMAGE, "rb") as f:
            image_bytes = f.read()

        # Mock boto3 so predict never touches real AWS.
        self._download_patch = patch.object(
            app_module, "download_image", lambda key: image_bytes
        )
        self._upload_patch = patch.object(
            app_module,
            "upload_image",
            lambda key, data, content_type="image/jpeg": key,
        )
        self._download_patch.start()
        self._upload_patch.start()

    def tearDown(self):
        self._download_patch.stop()
        self._upload_patch.stop()

    def test_predict_includes_processing_time(self):
        response = self.client.post(
            "/predict",
            json={"image_s3_key": "chat-1/pred-1/original/beatles.jpeg"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("time_took", data)
        self.assertIsInstance(data["time_took"], (int, float))
        self.assertGreaterEqual(data["time_took"], 0)
    
    def test_predict_rejects_non_image_file(self):
        response = self.client.post(
            "/predict",
            json={"image_s3_key": "chat-1/pred-1/original/notes.txt"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "Only image files are supported"
        )