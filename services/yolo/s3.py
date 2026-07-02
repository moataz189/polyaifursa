"""S3 helpers for the YOLO service.

All boto3 access lives here so the rest of the service never touches the SDK
directly. The bucket name and region come from the environment and are never
hard-coded:

    AWS_REGION       AWS region of the bucket (e.g. "us-east-1")
    AWS_S3_BUCKET    Name of the bucket to read/write images
"""

import os
import uuid

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


def download_image(key: str) -> bytes:
    """Download the object stored under `key` and return its raw bytes."""
    response = get_s3_client().get_object(Bucket=AWS_S3_BUCKET, Key=key)
    return response["Body"].read()


def upload_image(key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    """Upload raw image bytes to S3 under `key` and return the key."""
    get_s3_client().put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


def derive_predicted_key(original_key: str) -> str:
    """Turn an "original" image key into its "predicted" counterpart:

        <chat_id>/<prediction_id>/original/<image_name>
        -> <chat_id>/<prediction_id>/predicted/<image_name>
    """
    return original_key.replace("/original/", "/predicted/", 1)


def parse_prediction_id(key: str) -> str:
    """Extract the <prediction_id> segment from an object key. Falls back to a
    fresh UUID if the key does not follow the expected structure."""
    parts = key.split("/")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return str(uuid.uuid4())
