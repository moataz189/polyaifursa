import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Literal

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.fastmcp import FastMCP


Environment = Literal["dev", "prod"]

mcp = FastMCP("PolyAI Observability")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

DEV_S3_LOGS_BUCKET = os.getenv("DEV_S3_LOGS_BUCKET", "")
PROD_S3_LOGS_BUCKET = os.getenv("PROD_S3_LOGS_BUCKET", "")

DEV_PROMETHEUS_URL = os.getenv("DEV_PROMETHEUS_URL", "")
PROD_PROMETHEUS_URL = os.getenv("PROD_PROMETHEUS_URL", "")

s3_client = boto3.client("s3", region_name=AWS_REGION)


def get_bucket(environment: Environment) -> str:
    """Return the S3 bucket configured for the selected environment."""
    bucket = (
        DEV_S3_LOGS_BUCKET
        if environment == "dev"
        else PROD_S3_LOGS_BUCKET
    )

    if not bucket:
        raise ValueError(
            f"S3 logs bucket is not configured for environment '{environment}'"
        )

    return bucket


def get_prometheus_url(environment: Environment) -> str:
    """Return the Prometheus URL configured for the environment."""
    url = (
        DEV_PROMETHEUS_URL
        if environment == "dev"
        else PROD_PROMETHEUS_URL
    )

    if not url:
        raise ValueError(
            f"Prometheus URL is not configured for environment '{environment}'"
        )

    return url.rstrip("/")


def normalize_container_name(name: str) -> str:
    """
    Normalize a requested container name.

    Examples:
        yolo-service -> yolo
        yolo_service -> yolo
        services-yolo-1 -> yolo
    """
    normalized = name.strip().lower().replace("_", "-")

    if normalized.startswith("services-"):
        normalized = normalized.removeprefix("services-")

    if normalized.endswith("-service"):
        normalized = normalized.removesuffix("-service")

    # Remove a Docker Compose replica suffix such as "-1".
    parts = normalized.rsplit("-", maxsplit=1)

    if len(parts) == 2 and parts[1].isdigit():
        normalized = parts[0]

    return normalized


def container_names_match(requested: str, actual: str) -> bool:
    """Compare requested and stored container names using normalization."""
    return normalize_container_name(requested) == normalize_container_name(
        actual
    )


def decode_s3_log_object(data: bytes) -> str:
    """Decompress a gzip object when necessary and decode it as UTF-8."""
    try:
        # gzip files begin with bytes 1f 8b.
        if data.startswith(b"\x1f\x8b"):
            data = gzip.decompress(data)

        return data.decode("utf-8", errors="replace")

    except (OSError, EOFError) as exc:
        raise ValueError(f"Failed to decode gzip log object: {exc}") from exc


def iter_json_records(text: str) -> Iterator[dict[str, Any]]:
    """
    Yield JSON objects from a Fluent Bit file.

    Normally each record is stored on a separate line. The raw JSON decoder
    fallback also handles JSON objects written consecutively.
    """
    parsed_any_line = False

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(record, dict):
            parsed_any_line = True
            yield record

    if parsed_any_line:
        return

    decoder = json.JSONDecoder()
    position = 0

    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1

        if position >= len(text):
            break

        try:
            record, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break

        if isinstance(record, dict):
            yield record


def parse_log_timestamp(value: Any) -> datetime | None:
    """Parse the Docker JSON log timestamp as a UTC datetime."""
    if not isinstance(value, str) or not value:
        return None

    try:
        # Python supports microseconds, while Docker may output nanoseconds.
        # Keep at most six fractional digits.
        if "." in value:
            prefix, suffix = value.split(".", maxsplit=1)
            fraction = suffix.rstrip("Z")
            value = f"{prefix}.{fraction[:6]}+00:00"
        elif value.endswith("Z"):
            value = value[:-1] + "+00:00"

        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except ValueError:
        return None


def get_record_container_name(record: dict[str, Any]) -> str:
    """Read attrs.container_name from a Docker json-file record."""
    attrs = record.get("attrs", {})

    if not isinstance(attrs, dict):
        return ""

    value = attrs.get("container_name", "")
    return str(value) if value is not None else ""


def format_log_record(record: dict[str, Any]) -> str:
    """Convert a Docker JSON log record to a readable line."""
    timestamp = str(record.get("time", ""))
    stream = str(record.get("stream", ""))
    container_name = get_record_container_name(record)

    message = record.get("log", "")

    if not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=False)

    message = message.rstrip()

    details = [
        timestamp,
        f"[{container_name}]" if container_name else "",
        f"[{stream}]" if stream else "",
    ]

    prefix = " ".join(value for value in details if value)
    return f"{prefix} {message}".strip()


def build_date_prefixes(start: datetime, end: datetime) -> list[str]:
    """Build all S3 date prefixes required by a time range."""
    current_date = start.date()
    end_date = end.date()
    prefixes: list[str] = []

    while current_date <= end_date:
        prefixes.append(
            f"logs/{current_date.year:04d}/"
            f"{current_date.month:02d}/"
            f"{current_date.day:02d}/"
        )
        current_date += timedelta(days=1)

    return prefixes


def list_objects_for_period(
    bucket: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """List S3 log objects whose LastModified overlaps the requested period."""
    objects_by_key: dict[str, dict[str, Any]] = {}
    paginator = s3_client.get_paginator("list_objects_v2")

    for prefix in build_date_prefixes(start, end):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                modified = item.get("LastModified")

                if modified is None:
                    continue

                modified = modified.astimezone(timezone.utc)

                # Include a small buffer because an S3 object can contain logs
                # created before its upload time.
                if start - timedelta(minutes=2) <= modified <= end + timedelta(
                    minutes=2
                ):
                    objects_by_key[item["Key"]] = item

    return sorted(
        objects_by_key.values(),
        key=lambda item: item["LastModified"],
    )


def read_matching_logs(
    environment: Environment,
    container_name: str,
    start: datetime,
    end: datetime,
    max_lines: int,
) -> dict[str, Any]:
    """Read recent S3 files and return records matching a container and time."""
    bucket = get_bucket(environment)
    objects = list_objects_for_period(bucket, start, end)

    matching_lines: list[str] = []
    failed_objects: list[dict[str, str]] = []

    for item in objects:
        key = item["Key"]

        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            raw_data = response["Body"].read()
            text = decode_s3_log_object(raw_data)

            for record in iter_json_records(text):
                actual_container = get_record_container_name(record)

                if not container_names_match(
                    container_name,
                    actual_container,
                ):
                    continue

                log_time = parse_log_timestamp(record.get("time"))

                if log_time is not None and not (start <= log_time <= end):
                    continue

                matching_lines.append(format_log_record(record))

        except (ClientError, BotoCoreError, ValueError) as exc:
            failed_objects.append(
                {
                    "key": key,
                    "error": str(exc),
                }
            )

    if len(matching_lines) > max_lines:
        matching_lines = matching_lines[-max_lines:]

    return {
        "success": True,
        "environment": environment,
        "container": container_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "objects_scanned": len(objects),
        "matching_lines": len(matching_lines),
        "logs": matching_lines,
        "failed_objects": failed_objects,
    }


@mcp.tool()
def get_container_logs(
    container_name: str,
    minutes: int = 5,
    environment: Environment = "dev",
    max_lines: int = 200,
) -> dict[str, Any]:
    """
    Return logs for one container from the last selected number of minutes.

    The container name is read from attrs.container_name inside every
    Docker json-file record.

    Examples of accepted names:
    - yolo
    - yolo-service
    - agent-service
    - node-exporter-service
    """
    if not 1 <= minutes <= 1440:
        raise ValueError("minutes must be between 1 and 1440")

    if not 1 <= max_lines <= 2000:
        raise ValueError("max_lines must be between 1 and 2000")

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    try:
        return read_matching_logs(
            environment=environment,
            container_name=container_name,
            start=start,
            end=end,
            max_lines=max_lines,
        )
    except (ClientError, BotoCoreError, ValueError) as exc:
        return {
            "success": False,
            "environment": environment,
            "container": container_name,
            "error": str(exc),
        }


@mcp.tool()
def get_container_logs_at_time(
    container_name: str,
    timestamp: str,
    window_minutes: int = 5,
    environment: Environment = "dev",
    max_lines: int = 300,
) -> dict[str, Any]:
    """
    Investigate what happened to a container around a specific timestamp.

    Timestamp must use ISO-8601, for example:
    2026-07-01T12:00:00Z

    The tool searches before and after the supplied time.
    """
    if not 1 <= window_minutes <= 120:
        raise ValueError("window_minutes must be between 1 and 120")

    requested_time = parse_log_timestamp(timestamp)

    if requested_time is None:
        raise ValueError(
            "timestamp must be ISO-8601, for example "
            "'2026-07-01T12:00:00Z'"
        )

    start = requested_time - timedelta(minutes=window_minutes)
    end = requested_time + timedelta(minutes=window_minutes)

    try:
        return read_matching_logs(
            environment=environment,
            container_name=container_name,
            start=start,
            end=end,
            max_lines=max_lines,
        )
    except (ClientError, BotoCoreError, ValueError) as exc:
        return {
            "success": False,
            "environment": environment,
            "container": container_name,
            "timestamp": timestamp,
            "error": str(exc),
        }


@mcp.tool()
def list_log_containers(
    minutes: int = 60,
    environment: Environment = "dev",
) -> dict[str, Any]:
    """
    Show which containers have shipped logs to S3 recently.

    The names are discovered from attrs.container_name inside the logs.
    """
    if not 1 <= minutes <= 1440:
        raise ValueError("minutes must be between 1 and 1440")

    bucket = get_bucket(environment)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    containers: set[str] = set()

    try:
        objects = list_objects_for_period(bucket, start, end)

        for item in objects:
            response = s3_client.get_object(
                Bucket=bucket,
                Key=item["Key"],
            )

            text = decode_s3_log_object(response["Body"].read())

            for record in iter_json_records(text):
                container_name = get_record_container_name(record)

                if container_name:
                    containers.add(container_name)

        return {
            "success": True,
            "environment": environment,
            "minutes": minutes,
            "objects_scanned": len(objects),
            "containers": sorted(containers),
            "count": len(containers),
        }

    except (ClientError, BotoCoreError, ValueError) as exc:
        return {
            "success": False,
            "environment": environment,
            "error": str(exc),
        }


@mcp.tool()
def query_prometheus(
    query: str,
    environment: Environment = "dev",
) -> dict[str, Any]:
    """Run an instant PromQL query against Prometheus."""
    prometheus_url = get_prometheus_url(environment)

    try:
        response = requests.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query},
            timeout=15,
        )
        response.raise_for_status()

        payload = response.json()

        return {
            "success": payload.get("status") == "success",
            "environment": environment,
            "query": query,
            "result": payload.get("data", {}).get("result", []),
            "error": payload.get("error"),
        }

    except (requests.RequestException, ValueError) as exc:
        return {
            "success": False,
            "environment": environment,
            "query": query,
            "error": str(exc),
        }


@mcp.tool()
def get_cpu_usage(
    minutes: int = 10,
    environment: Environment = "dev",
    step_seconds: int = 20,
) -> dict[str, Any]:
    """
    Return EC2 CPU usage percentage from Prometheus for a time range.

    It uses node_exporter's node_cpu_seconds_total metric.
    """
    if not 1 <= minutes <= 1440:
        raise ValueError("minutes must be between 1 and 1440")

    if not 5 <= step_seconds <= 3600:
        raise ValueError("step_seconds must be between 5 and 3600")

    prometheus_url = get_prometheus_url(environment)

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    query = (
    '100 * (1 - avg(rate('
    'node_cpu_seconds_total{'
    'mode="idle",'
    'instance="node-exporter:9100",'
    'job="node-exporter"'
    '}[1m])))'
)

    try:
        response = requests.get(
            f"{prometheus_url}/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step_seconds,
            },
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()

        return {
            "success": payload.get("status") == "success",
            "environment": environment,
            "minutes": minutes,
            "query": query,
            "result": payload.get("data", {}).get("result", []),
            "error": payload.get("error"),
        }

    except (requests.RequestException, ValueError) as exc:
        return {
            "success": False,
            "environment": environment,
            "minutes": minutes,
            "error": str(exc),
        }


if __name__ == "__main__":
    # VS Code starts this process through .vscode/mcp.json.
    # FastMCP uses stdio by default.
    mcp.run()