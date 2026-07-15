"""S3 helpers for the Agent service.

All boto3 access lives here so the rest of the service never touches the SDK
directly. The bucket name and region come from the environment and are never
hard-coded:

    AWS_REGION       AWS region of the bucket (e.g. "us-east-1")
    AWS_S3_BUCKET    Name of the bucket to read/write images
"""

import os
import uuid
from typing import Optional

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET")

# A single client is reused across requests instead of building one per call.
_s3_client = None


def get_s3_client():
    """Return a lazily-created, shared boto3 S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


def safe_image_name(filename: Optional[str], fallback: Optional[str] = None) -> str:
    """Return a safe single-segment filename for use in an S3 object key.

    Preserves the original uploaded filename and its extension, but uses
    os.path.basename() to strip any directory components so the value cannot
    inject extra path segments (or a leading slash) into the key. When no usable
    filename is supplied it returns `fallback` (e.g. "<image_id>.jpg"), or a
    generated name if no fallback was provided.
    """
    name = os.path.basename((filename or "").strip())
    if not name or name in {".", ".."}:
        return fallback or f"{uuid.uuid4().hex}.jpg"
    return name


def build_object_key(chat_id: str, image_id: str, stage: str, image_name: str) -> str:
    """Build an S3 object key with the recommended structure:

        <chat_id>/<image_id>/<stage>/<image_name>

    `stage` is "original" or "processed". The <image_id> identifies the uploaded
    image flow and is distinct from a YOLO prediction_uid.
    """
    return f"{chat_id}/{image_id}/{stage}/{image_name}"


def upload_image(key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    """Upload raw image bytes to S3 under `key` and return the key."""
    get_s3_client().put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


def download_image(key: str) -> bytes:
    """Download the object stored under `key` and return its raw bytes."""
    response = get_s3_client().get_object(Bucket=AWS_S3_BUCKET, Key=key)
    return response["Body"].read()
