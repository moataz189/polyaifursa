"""S3 helpers for the YOLO service.

All boto3 access lives here so the rest of the service never touches the SDK
directly. The bucket name and region come from the environment and are never
hard-coded:

    AWS_REGION       AWS region of the bucket (e.g. "us-east-1")
    AWS_S3_BUCKET    Name of the bucket to read/write images
"""

import os
import posixpath
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

        <chat_id>/<image_id>/original/<image_name>
        -> <chat_id>/<image_id>/predicted/<image_name>

    Kept for backwards compatibility. New code should use build_annotated_key,
    which stores annotated images under predictions/<prediction_uid>/.
    """
    return original_key.replace("/original/", "/predicted/", 1)


def build_annotated_key(source_key: str, prediction_uid: str) -> str:
    """Build the S3 key for a YOLO annotated (predicted) image.

    The annotated image lives under a per-prediction folder so repeated
    detections of the same image never collide:

        <chat_id>/<image_id>/original/<name>.<ext>
        -> <chat_id>/<image_id>/predictions/<prediction_uid>/annotated_<name>.png

    The trailing stage segment ("original" or "processed") of the source key is
    dropped to recover the <chat_id>/<image_id> prefix. Keys that do not follow
    the expected structure keep their directory and just gain the
    predictions/<prediction_uid>/ suffix.
    """
    directory, filename = posixpath.split(source_key)
    stem, _ext = posixpath.splitext(filename)

    parts = [p for p in directory.split("/") if p] if directory else []
    if parts and parts[-1] in ("original", "processed"):
        parts = parts[:-1]

    annotated_name = f"annotated_{stem}.png"
    return "/".join(parts + ["predictions", prediction_uid, annotated_name])


def parse_image_id(key: str) -> str:
    """Extract the <image_id> segment from an object key. Falls back to a
    fresh UUID if the key does not follow the expected structure."""
    parts = key.split("/")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return str(uuid.uuid4())


# Backwards-compatible alias: the S3 folder id used to be called prediction_id.
parse_prediction_id = parse_image_id
