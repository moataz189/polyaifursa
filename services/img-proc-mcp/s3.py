"""S3 helpers for the image-processing MCP server.

All boto3 access lives here so the rest of the server never touches the SDK
directly. The bucket name and region come from the environment and are never
hard-coded:

    AWS_REGION            AWS region of the bucket (e.g. "us-east-1")
    S3_BUCKET             Name of the bucket to read/write images
    S3_PROCESSED_PREFIX   Key prefix for processed images (default "processed/")
"""

import os
import posixpath
import re

import boto3
from dotenv import load_dotenv

load_dotenv()
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_PROCESSED_PREFIX = os.environ.get("S3_PROCESSED_PREFIX", "processed/")

# A single client is reused across calls instead of building one per request.
_s3_client = None


def get_s3_client():
    """Return a lazily-created, shared boto3 S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


def download_image(key: str) -> bytes:
    """Download the object stored under `key` and return its raw bytes."""
    response = get_s3_client().get_object(Bucket=S3_BUCKET, Key=key)
    return response["Body"].read()


def upload_image(key: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload raw image bytes to S3 under `key` and return the key."""
    get_s3_client().put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


def build_processed_key(input_key: str, descriptor: str) -> str:
    """Build the output S3 key for a processed image.

    Preserves the original hierarchy of `input_key` (e.g. the
    <chat_id>/<prediction_id> prefix), replaces the trailing "original"
    directory with "processed", and names the file
    "<descriptor>_<original_stem>.png":

        <chat_id>/<prediction_id>/original/test.jpeg
        -> <chat_id>/<prediction_id>/processed/<descriptor>_test.png

    If the input key has no "original" directory, a "processed" directory is
    appended after the input's directory (or the configured
    S3_PROCESSED_PREFIX is used when the input key has no directory at all).
    """
    directory, filename = posixpath.split(input_key)
    stem, _ext = posixpath.splitext(filename)

    parts = [p for p in directory.split("/") if p] if directory else []
    if parts and parts[-1] == "original":
        parts[-1] = "processed"
    elif parts:
        parts.append("processed")
    else:
        parts = [S3_PROCESSED_PREFIX.strip("/")]

    safe_name = sanitize_segment(f"{descriptor}_{stem}") + ".png"
    return "/".join(parts + [safe_name])


def sanitize_segment(text: str) -> str:
    """Return an S3-safe version of a single object-key path segment.

    Spaces become underscores and any character outside [A-Za-z0-9._-] is
    dropped, so the result is always a valid, predictable object-key segment.
    """
    text = text.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "", text)

