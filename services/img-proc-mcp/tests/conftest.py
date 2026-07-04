"""Shared pytest fixtures for the image-processing MCP server tests."""

import io
import os
import sys

import pytest
from PIL import Image

# Make app.py / s3.py importable when running from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import s3  # noqa: E402


def _make_png_bytes(width: int = 16, height: int = 16) -> bytes:
    """Return the PNG bytes of a small solid-colour test image."""
    image = Image.new("RGB", (width, height), color=(120, 60, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def mock_s3(monkeypatch):
    """Mock S3 download/upload so tools never touch real AWS.

    download returns a small in-memory PNG; upload records the bytes it was
    asked to store, keyed by the object key, and returns the key.
    """
    uploaded = {}

    monkeypatch.setattr(s3, "download_image", lambda key: _make_png_bytes())
    monkeypatch.setattr(
        s3,
        "upload_image",
        lambda key, data, content_type="image/png": uploaded.setdefault(key, data) or key,
    )
    return uploaded
