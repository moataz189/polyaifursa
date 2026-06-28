import os
import unittest
import tempfile
from unittest.mock import patch
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


class TestPredictionTime(unittest.TestCase):
    def setUp(self):
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        init_db()
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