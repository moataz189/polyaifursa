import asyncio
import base64
import io
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("langchain").setLevel(logging.DEBUG)
logging.getLogger("langchain_core").setLevel(logging.DEBUG)

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_core.tools import tool
from PIL import Image
from pydantic import BaseModel

from mcp_client import get_mcp_tools
from s3 import build_object_key, download_image, safe_image_name, upload_image

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
YOLO_PUBLIC_URL = os.getenv("YOLO_PUBLIC_URL", YOLO_SERVICE_URL)
MODEL = os.environ.get("MODEL")
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "bedrock_converse")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Text-only models
ALLOWED_MODELS = {
    "anthropic.claude-3-haiku-20240307-v1:0",
    "amazon.nova-micro-v1:0",
    "amazon.nova-lite-v1:0",
    "openai.gpt-oss-20b-1:0",
    "meta.llama3-1-8b-instruct-v1:0",
    "mistral.mistral-7b-instruct-v0:2",
    
}

if MODEL not in ALLOWED_MODELS:
    allowed_list = "\n  ".join(sorted(ALLOWED_MODELS))
    raise SystemExit(
        f"\n[ERROR] MODEL='{MODEL}' is not allowed.\n"
        f"Set MODEL in your .env to one of the supported text-only models:\n  {allowed_list}\n"
    )

SYSTEM_PROMPT = (
    """
You are an AI vision assistant.

Use tools when needed:
- detect_objects: detect objects in the selected image.
- show_annotated_image: show bounding boxes after detection.
- MCP image tools: rotate, flip, blur, resize, crop, add_noise.

Image keys:
- original image -> use original_image_s3_key
- current/latest image -> use latest_image_s3_key
- processed image -> use latest_processed_image_s3_key if available

For MCP image tools, always pass input_key.

Rules:
1. If the user asks to detect/analyze/identify objects, call detect_objects.
2. If the user asks for annotated/bounding boxes, call show_annotated_image after detection.
3. If the user asks for whole-image editing, call the MCP tool directly.
4. If the user asks to edit a specific object:
   detect_objects -> select_object -> MCP tool with left/top/right/bottom.
5. For object rotation:
   detect_objects -> select_object -> rotate with input_key, angle, left, top, right, bottom.
   180 degrees is allowed for any object region.
   90/270 degrees works only for square regions.

Do not ask for confirmation when the request is clear.
Do not print image URLs; the frontend displays images automatically.
    """
)

_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)
_current_image_id: ContextVar[Optional[str]] = ContextVar("current_image_id", default=None)
# The original uploaded image key (chat_id/image_id/original/<filename>) for the
# current image flow. Stays fixed while image-processing tools produce new keys.
_original_image_s3_key: ContextVar[Optional[str]] = ContextVar("original_image_s3_key", default=None)
# The most recent processed image key (None until an image-processing tool runs).
_latest_processed_key: ContextVar[Optional[str]] = ContextVar("latest_processed_key", default=None)
_latest_prediction_uid: ContextVar[Optional[str]] = ContextVar("latest_prediction_uid", default=None)


# Backend-side chat state, keyed by chat_id. The frontend only sends chat_id +
# messages; the server remembers the image flow (S3 keys, image_id, prediction
# id) between requests so those keys are NEVER exposed to the frontend.
# NOTE: in-memory and per-process. If the agent runs with multiple workers,
# move this to shared storage (e.g. Redis or a database) keyed by chat_id.
_chat_state: dict[str, dict] = {}


def _resolve_detect_source(source: Optional[str]) -> Optional[str]:
    """Resolve which image key a detection should run on.

    - "original":  the originally uploaded image (chat_id/image_id/original/...).
    - "processed": the most recent processed image, if any.
    - "current"/default/anything else: the latest usable image key.

    Falls back to the current/latest image key when the requested source is not
    available (e.g. "processed" before any processing has happened).
    """
    choice = (source or "current").strip().lower()
    current = _current_image_s3_key.get()
    if choice == "original":
        return _original_image_s3_key.get() or current
    if choice == "processed":
        return _latest_processed_key.get() or current
    return current


@tool
def detect_objects(source: str = "current") -> str:
    """Detect and identify objects in an image using YOLO object detection.

    `source` selects which image to analyze:
    - "current" (default): the latest image in play (the most recent
      uploaded OR processed image). Use this when the user just says
      "detect the image" or does not specify.
    - "original": the image the user originally uploaded, ignoring any
      processing. Use for "detect the original image".
    - "processed": the most recent processed image (e.g. after rotate/blur/
      flip/resize/crop/noise). Use for "detect the rotated/blurred/processed image".
    """
    image_s3_key = _resolve_detect_source(source)

    if not image_s3_key:
        return json.dumps({"error": "No image was provided by the user."})

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            json={"image_s3_key": image_s3_key},
        )
        response.raise_for_status()

    data = response.json()
    # Echo back which key was detected so the caller can track that the
    # prediction belongs to this image key.
    data["detected_image_s3_key"] = image_s3_key
    return json.dumps(data)


@tool
def show_annotated_image() -> str:
    """Return the URL of the annotated image (the picture with bounding boxes drawn on it)
    from the most recent object detection.

    Use this ONLY when the user explicitly asks to see the annotated image / the image with boxes.
    You must run detect_objects first so a detection result exists."""
    prediction_uid = _latest_prediction_uid.get()

    if not prediction_uid:
        return json.dumps(
            {"error": "No detection has been run yet. Run detect_objects first, then try again."}
        )

    image_url = f"{YOLO_PUBLIC_URL}/prediction/{prediction_uid}/image"
    return json.dumps({"image_url": image_url})


def _parse_box(box):
    """Return (left, top, right, bottom) as floats from a detection's box.

    YOLO stores a box either as a list [l, t, r, b] or its string form
    (e.g. "[12.0, 30.5, 88.0, 120.0]"); accept both.
    """
    if isinstance(box, str):
        box = json.loads(box)
    left, top, right, bottom = box
    return float(left), float(top), float(right), float(bottom)


def select_object_bbox(detections, label=None, index=1, direction="from_left"):
    """Pick one detection's bounding box by label, ordinal index and direction.

    - `detections`: list of dicts, each with a "label" and a "box"
      ([left, top, right, bottom], or the string form of that list).
    - `label`: keep only detections whose label matches (case-insensitive);
      None keeps every detection.
    - `index`: 1-based ordinal ("first" = 1, "second" = 2, ...).
    - `direction`: "from_left" orders objects by ascending left coordinate,
      "from_right" by descending left coordinate.

    Returns a dict {"left", "top", "right", "bottom"} of ints. Raises ValueError
    when the request cannot be satisfied.
    """
    if direction not in ("from_left", "from_right"):
        raise ValueError("direction must be 'from_left' or 'from_right'")
    if index < 1:
        raise ValueError("index must be 1 or greater")

    matches = []
    for det in detections:
        if label is not None and str(det.get("label", "")).lower() != label.lower():
            continue
        matches.append(_parse_box(det["box"]))

    if not matches:
        raise ValueError(f"no detections found for label {label!r}")

    # Order left-to-right (or right-to-left) by the box's left coordinate.
    matches.sort(key=lambda b: b[0], reverse=(direction == "from_right"))

    if index > len(matches):
        raise ValueError(
            f"requested object #{index} but only {len(matches)} match(es) found"
        )

    left, top, right, bottom = matches[index - 1]
    return {
        "left": int(round(left)),
        "top": int(round(top)),
        "right": int(round(right)),
        "bottom": int(round(bottom)),
    }


@tool
def select_object(
    label: Optional[str] = None, index: int = 1, direction: str = "from_left"
) -> str:
    """Select one detected object's bounding box from the most recent detection.

    Run detect_objects FIRST. Then use this to pick a specific object by:
    - `label`: object class, e.g. "dog", "car", "person" (None = any object).
    - `index`: 1-based ordinal ("first" = 1, "second" = 2, ...).
    - `direction`: "from_left" or "from_right" (objects are ordered by their
      horizontal position / left edge).

    Returns JSON with the chosen box: {"left", "top", "right", "bottom"}. Pass
    these coordinates to blur/add_noise/crop to process ONLY that object.
    """
    prediction_uid = _latest_prediction_uid.get()
    if not prediction_uid:
        return json.dumps(
            {"error": "No detection has been run yet. Run detect_objects first."}
        )

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{YOLO_SERVICE_URL}/prediction/{prediction_uid}")
            response.raise_for_status()
        detections = response.json().get("detection_objects", [])
        box = select_object_bbox(
            detections, label=label, index=index, direction=direction
        )
    except ValueError as exc:  # no match / bad index / bad direction
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # network / transport boundary
        return json.dumps({"error": str(exc)})

    return json.dumps(box)


# Local YOLO / business-logic tools implemented in this module. The
# image-processing tools (rotate, flip, blur, resize, crop, add_noise) are NOT
# defined here; they are discovered from the img-proc MCP server over HTTP and
# combined with the local tools at startup (see ALL_TOOLS below).
LOCAL_TOOLS = [detect_objects, show_annotated_image, select_object]

# Throttle outbound LLM requests. Realistic values for a single-user dev
# deployment: ~1 request/sec with a small burst allowance.
rate_limiter = InMemoryRateLimiter(
    requests_per_second=1,
    check_every_n_seconds=0.1,
    max_bucket_size=5,
)

llm = init_chat_model(
    MODEL,
    model_provider=MODEL_PROVIDER,
    region_name=AWS_REGION,rate_limiter=rate_limiter
)


def _discover_mcp_tools() -> list:
    """Discover the img-proc MCP tools over HTTP.

    Best-effort: if the MCP server is unreachable at startup the agent still
    runs with its local YOLO tools, and a warning is logged.
    """
    try:
        return get_mcp_tools()
    except Exception as exc:  # discovery / transport boundary
        logging.warning("Failed to discover image-processing MCP tools: %s", exc)
        return []


# Discover the image-processing tools from the MCP server and work directly with
# the combined tool list. There is no manual name -> tool registry; run_agent
# looks tools up straight from ALL_TOOLS.
mcp_tools = _discover_mcp_tools()
ALL_TOOLS = LOCAL_TOOLS + mcp_tools

# Tool names that produce a processed image in S3 (their result is the processed
# image's S3 key). Used ONLY for post-processing: their result is downloaded and
# returned to the client as base64 (processed_image).
IMG_PROC_TOOL_NAMES = {t.name for t in mcp_tools}

if mcp_tools:
    logging.info(
        "Discovered %d image-processing MCP tool(s): %s",
        len(mcp_tools),
        ", ".join(sorted(IMG_PROC_TOOL_NAMES)),
    )

# The LLM is bound to the local tools plus the discovered MCP tools.
llm_with_tools = llm.bind_tools(ALL_TOOLS)


def _encode_image_b64(data: bytes) -> str:
    """Encode raw image bytes as an ASCII base64 string."""
    return base64.b64encode(data).decode("ascii")


# Longest-side pixel limit for images returned to the frontend. Large images
# (15MB+ raw) can choke Chrome DevTools and the chat UI, so we downscale a
# DISPLAY copy only. The original stored in S3 is never modified.
DISPLAY_MAX_SIDE = 1200


def _resize_for_display(data: bytes) -> bytes:
    """Return display-friendly image bytes, downscaled if the longest side
    exceeds DISPLAY_MAX_SIDE. Aspect ratio is preserved.

    Only the copy returned to the frontend is affected; the image stored in S3
    is untouched. Images with transparency are re-encoded as PNG; everything
    else becomes JPEG (much smaller for photos). On any failure the original
    bytes are returned unchanged so display still works.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            longest = max(width, height)
            if longest <= DISPLAY_MAX_SIDE:
                return data

            scale = DISPLAY_MAX_SIDE / longest
            new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            resized = image.resize(new_size, Image.Resampling.LANCZOS)

            has_alpha = resized.mode in ("RGBA", "LA") or (
                resized.mode == "P" and "transparency" in resized.info
            )
            buffer = io.BytesIO()
            if has_alpha:
                resized.convert("RGBA").save(buffer, format="PNG", optimize=True)
            else:
                resized.convert("RGB").save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()
    except Exception as exc:  # image decoding boundary
        logging.warning("Failed to resize image for display: %s", exc)
        return data


def _fetch_annotated_image_b64(prediction_uid: Optional[str]) -> Optional[str]:
    """Download the annotated image for a prediction from YOLO and return it
    as a base64 string. Returns None if there is no prediction or the fetch fails.

    This runs outside the LLM message flow so the model never sees image data.
    """
    if not prediction_uid:
        return None

    url = f"{YOLO_SERVICE_URL}/prediction/{prediction_uid}/image"
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logging.warning("Failed to fetch annotated image from %s: %s", url, exc)
        return None

    logging.info("Fetched annotated image for prediction %s (%d bytes)", prediction_uid, len(response.content))
    return _encode_image_b64(_resize_for_display(response.content))


def _fetch_processed_image_b64(output_key: Optional[str]) -> Optional[str]:
    """Download a processed image from S3 by its output key and return it as a
    base64 string. Returns None if there is no key or the download fails.

    This runs outside the LLM message flow so the model never sees image data.
    """
    if not output_key:
        return None

    try:
        data = download_image(output_key)
    except Exception as exc:  # S3 access boundary
        logging.warning("Failed to download processed image %s: %s", output_key, exc)
        return None

    logging.info("Fetched processed image %s (%d bytes)", output_key, len(data))
    return _encode_image_b64(_resize_for_display(data))


def _build_key_context_message(
    latest_key: Optional[str],
    original_key: Optional[str],
    processed_key: Optional[str],
) -> Optional[str]:
    """Build the per-request system message that lists the available image S3
    keys, so the model can pass the right `input_key` to the MCP tools.

    Returns None when no image key is available (nothing to process yet).
    """
    lines = []
    if latest_key:
        lines.append(f"- latest_image_s3_key (current/latest image): {latest_key}")
    if original_key:
        lines.append(f"- original_image_s3_key (original uploaded image): {original_key}")
    if processed_key:
        lines.append(
            f"- latest_processed_image_s3_key (most recent processed image): {processed_key}"
        )
    if not lines:
        return None

    return (
        "Available image S3 keys for this request. When you call an image-processing "
        "tool (rotate, flip, blur, resize, crop, add_noise), pass the correct key as the "
        "tool's `input_key` argument:\n" + "\n".join(lines)
    )


def _extract_output_key(content) -> Optional[str]:
    """Return the processed image's S3 key from an image-processing tool result.

    The MCP tools return the output S3 key directly as their text result, but we
    also accept a JSON object with an "output_key" field for robustness. MCP
    tools may also return a list of content blocks, e.g.
    ``[{"type": "text", "text": "chat/img/processed/crop.png"}]`` — in that case
    we use the text of the first block whose type is "text".
    """
    if not content:
        return None

    # MCP content-block list: pick the first text block's text.
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return _extract_output_key(block.get("text"))
        return None

    text = content if isinstance(content, str) else str(content)
    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text

    if isinstance(data, dict):
        return data.get("output_key")
    if isinstance(data, list):
        return _extract_output_key(data)
    if isinstance(data, str):
        return data or None
    return text


def run_agent(history: list, max_iterations: int = 10) -> dict:
    """
    Simple ReAct loop:
      1. Send messages to the LLM.
      2. If the LLM requests tool calls, execute them and append results.
      3. Repeat until the LLM returns a plain text response.
      4. Stop after max_iterations to prevent infinite loops.

    Returns a dict with the final answer plus metadata about the loop.
    """
    start_time = time.perf_counter()
    messages = [SystemMessage(content=SYSTEM_PROMPT)]
    # Tell the model which S3 keys are in play for this request so it can pass
    # the correct `input_key` directly to the MCP image-processing tools.
    key_context = _build_key_context_message(
        latest_key=_current_image_s3_key.get(),
        original_key=_original_image_s3_key.get(),
        processed_key=_latest_processed_key.get(),
    )
    if key_context:
        messages.append(SystemMessage(content=key_context))
    messages += history
    image_url = None
    annotated_image = None
    processed_image = None
    # The latest usable image key for this loop. Seeded from the context var
    # (set in chat() to the newly uploaded image or the key carried over from
    # an earlier request). When an image-processing tool produces a new image,
    # this is updated so later tools chain on the processed result and the key
    # round-trips to the client for the next request.
    latest_image_s3_key = _current_image_s3_key.get()
    # The image_id of the current image flow. Stays fixed while image-processing
    # tools produce new keys, and round-trips to the client so follow-up
    # requests keep operating on the same image flow.
    latest_image_id = _current_image_id.get()
    # The original uploaded image key of the current flow. Stays fixed while
    # image-processing tools produce new keys, and round-trips to the client so
    # "detect the original image" keeps resolving to the true original.
    original_image_s3_key = _original_image_s3_key.get()
    # Seed from the context var, which holds request.latest_prediction_id (set
    # in chat() before this runs). This lets a follow-up "show annotated image"
    # request fetch the image for a detection that ran in an EARLIER request,
    # where detect_objects does not run again. It also makes the returned
    # prediction_id round-trip so the client keeps a valid id.
    prediction_uid = _latest_prediction_uid.get()
    # The image key the current prediction belongs to. A seeded prediction is
    # assumed to belong to the seeded image key (the frontend keeps them in
    # sync). This lets show_annotated_image refuse to show an annotation from a
    # DIFFERENT (older) image than the one currently in play.
    prediction_image_key = latest_image_s3_key if prediction_uid else None
    # True once detect_objects produces a prediction IN THIS run. A prediction
    # created this run is always valid for show_annotated_image, even when the
    # detection ran on the "original" source while latest_image_s3_key points at
    # a processed image. The stale guard only applies to predictions reused from
    # an earlier request.
    prediction_created_this_run = False
    tools_called: list[str] = []
    iterations = 0
    context_limit_exceeded = False
    answer = "Agent stopped: maximum iterations reached."
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0

    while iterations < max_iterations:
        iterations += 1

        response: AIMessage = llm_with_tools.invoke(messages)

        # Some providers (e.g. Bedrock via gpt-oss) suffix the tool name with
        # control tokens like "show_annotated_image<|channel|>commentary".
        # Bedrock requires toolUse.name to match [a-zA-Z0-9_-]+, so sanitize the
        # names ON THE RESPONSE before it is appended to the history; otherwise
        # the invalid name is echoed back on the next request and rejected.
        for tool_call in response.tool_calls:
            tool_call["name"] = tool_call["name"].split("<|")[0]
            # Bedrock Converse requires toolUse.input to be a JSON object. Our
            # tools take no arguments, so the model may emit None/"" for args;
            # coerce anything that is not a dict to {} so the echoed-back
            # AIMessage stays valid on the next request.
            if not isinstance(tool_call.get("args"), dict):
                tool_call["args"] = {}

        messages.append(response)

        # Accumulate token usage across every LLM call in the loop.
        usage = response.usage_metadata
        if usage:
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
            total_tokens += usage.get("total_tokens", 0)

        # No tool calls, the model produced its final answer
        if not response.tool_calls:
            # Some providers (e.g. Bedrock Converse) return content as a list of
            # blocks (reasoning + text) instead of a plain string. `.text`
            # concatenates the text blocks and drops reasoning, giving us a str.
            answer = response.text
            break

        # Execute every tool the model requested
        
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]

            # Look the tool up directly from the combined tool list (no manual
            # name -> tool registry).
            tool_fn = next((t for t in ALL_TOOLS if t.name == tool_name), None)
            if tool_fn is None:
                messages.append(
                    ToolMessage(
                        content=json.dumps({"error": f"Unknown tool: {tool_name}"}),
                        tool_call_id=tool_call["id"],
                    )
                )
                continue

            # Discovered MCP tools are async-only; local YOLO tools are sync.
            if tool_name in IMG_PROC_TOOL_NAMES:
                tool_result = asyncio.run(tool_fn.ainvoke(tool_call))
            else:
                tool_result = tool_fn.invoke(tool_call)

            messages.append(tool_result)
            tools_called.append(tool_name)

            if tool_name == "detect_objects":
                # Store the UID in THIS context so a later show_annotated_image
                # call (which runs in a child context) can read it.
                tool_data = json.loads(tool_result.content)
                uid = tool_data.get("prediction_uid")
                if uid:
                    prediction_uid = uid
                    # The detection belongs to the image key it actually ran on
                    # (which may be the current, original, or processed image).
                    prediction_image_key = (
                        tool_data.get("detected_image_s3_key") or latest_image_s3_key
                    )
                    # A prediction created this run is always valid for
                    # show_annotated_image, even when it was detected on the
                    # "original" source while the latest image is processed.
                    prediction_created_this_run = True
                    _latest_prediction_uid.set(uid)


            if tool_name == "show_annotated_image":
                # Show the annotation when we have a prediction that is valid for
                # the image currently in play. A prediction CREATED THIS RUN is
                # always valid (it may have been detected on the "original"
                # source while latest_image_s3_key is a processed image). For a
                # prediction REUSED from an earlier request, keep the stale guard
                # so an older image's annotation is not surfaced after a newer
                # image was uploaded or processed.
                if prediction_uid and (
                    prediction_created_this_run
                    or prediction_image_key == latest_image_s3_key
                ):
                    tool_data = json.loads(tool_result.content)
                    image_url = tool_data.get("image_url") or image_url
                    # Fetch the annotated image bytes from YOLO and base64-encode
                    # them here, OUTSIDE the LLM message flow, so the model never
                    # sees image data (text-only architecture constraint).
                    annotated_image = _fetch_annotated_image_b64(prediction_uid) or annotated_image

            if tool_name in IMG_PROC_TOOL_NAMES:
                # Image-processing MCP tools return the S3 key of the processed
                # image as their result. Download and base64-encode it here,
                # OUTSIDE the LLM message flow, so the client can display it (the
                # model never sees image data).
                output_key = _extract_output_key(tool_result.content)
                if output_key:
                    processed_image = _fetch_processed_image_b64(output_key) or processed_image
                    # The processed image becomes the latest usable image, so a
                    # follow-up tool (this loop or a later request) operates on
                    # it. The image_id stays the same (same image flow). Update
                    # the context vars too so subsequent tool calls in THIS loop
                    # read the new key and can detect the processed image.
                    latest_image_s3_key = output_key
                    _current_image_s3_key.set(output_key)
                    _latest_processed_key.set(output_key)
                    # The processed image has no YOLO detection yet, so the old
                    # prediction no longer applies. Reset it so a follow-up
                    # "show annotated image" cannot surface the previous image's
                    # result.
                    prediction_uid = None
                    prediction_image_key = None
                    prediction_created_this_run = False
                    _latest_prediction_uid.set(None)
    else:
        # The while loop finished without `break`, meaning we hit max_iterations.
        context_limit_exceeded = True

    agent_loop_time_s = round(time.perf_counter() - start_time, 3)

    return {
        "response": answer,
        "image_url": image_url,
        "annotated_image": annotated_image,
        "processed_image": processed_image,
        "prediction_id": prediction_uid,
        "latest_image_s3_key": latest_image_s3_key,
        "latest_image_id": latest_image_id,
        "original_image_s3_key": original_image_s3_key,
        "iterations": iterations,
        "tools_called": tools_called,
        "context_limit_exceeded": context_limit_exceeded,
        "agent_loop_time_s": agent_loop_time_s,
        "tokens_used": {
            "input": input_tokens,
            "output": output_tokens,
            "total": total_tokens,
        },
    }

app = FastAPI(title="Vision Agent")

app.add_middleware(
    CORSMiddleware,
     allow_origins=[
        "http://moataz-prod.fursa.click:3000",
        "http://moataz-dev.fursa.click:3000","http://localhost:3000"
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


class ChatMessage(BaseModel):
    role: str                           # "user" or "assistant"
    content: str
    image_base64: Optional[str] = None  # only on user messages that carry an image
    image_filename: Optional[str] = None  # original uploaded filename (e.g. "photo.png")


class ChatRequest(BaseModel):
    chat_id: str                        # stable id generated once by the client
    messages: list[ChatMessage]         # full conversation thread, oldest first


class TokensUsed(BaseModel):
    input: int
    output: int
    total: int


class ChatResponse(BaseModel):
    response: str
    prediction_id: str | None = None
    annotated_image: str | None = None
    processed_image: str | None = None
    image_url: str | None = None        # backward-compatible annotated-image URL
    agent_loop_time_s: float
    iterations: int
    tools_called: list[str]
    context_limit_exceeded: bool
    tokens_used: TokensUsed


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    chat_id = request.chat_id

    # Load this conversation's remembered image state (S3 keys, image_id,
    # prediction id) from the backend store. The frontend never sees or sends
    # these; they are looked up by chat_id here.
    state = _chat_state.get(chat_id, {})

    # Build the text-only conversation history for the LLM. Image bytes are
    # never sent to the model; we only mark that an image was attached.
    lc_messages = []
    for msg in request.messages:
        if msg.role == "user":
            if msg.image_base64:
                content = msg.content + "\n[An image was uploaded. Use existing tools to analyze it according to user instructions.]"
            else:
                content = msg.content
            lc_messages.append(HumanMessage(content=content))
        else:
            lc_messages.append(AIMessage(content=msg.content))

    # Upload ONLY the newest image: the one attached to the most recent user
    # message. Older images were already uploaded on previous requests, so we
    # never re-upload them here.
    latest_image_s3_key = None
    latest_image_id = None
    latest_user_msg = next(
        (m for m in reversed(request.messages) if m.role == "user"), None
    )
    if latest_user_msg and latest_user_msg.image_base64:
        # Each new uploaded image gets its own image_id under the stable chat_id.
        # This identifies the image flow and is distinct from a YOLO
        # prediction_uid.
        latest_image_id = str(uuid.uuid4())
        image_bytes = base64.b64decode(latest_user_msg.image_base64)
        # Preserve the real uploaded filename in the key; if the client did not
        # send one, fall back to "<image_id>.jpg".
        image_name = safe_image_name(
            latest_user_msg.image_filename, fallback=f"{latest_image_id}.jpg"
        )
        latest_image_s3_key = build_object_key(
            chat_id, latest_image_id, "original", image_name
        )
        upload_image(latest_image_s3_key, image_bytes)

    # The key the tools should operate on: the freshly uploaded image if there
    # is one, otherwise the last image key remembered for this chat from an
    # earlier request. This lets follow-ups like "rotate the previous image"
    # work without re-uploading.
    current_image_s3_key = latest_image_s3_key or state.get("latest_image_s3_key")
    # The image_id of the current image flow: the freshly generated one, or the
    # one remembered from an earlier request.
    current_image_id = latest_image_id or state.get("latest_image_id")
    # The ORIGINAL image key of the current flow. On a fresh upload this is the
    # just-uploaded original key; otherwise it is the one remembered from an
    # earlier request. It is NEVER derived from latest_image_s3_key, which may
    # point at a processed image after image-processing tools ran.
    current_original_image_s3_key = latest_image_s3_key or state.get("original_image_s3_key")

    # A newly uploaded image has NOT been detected yet, so any remembered
    # prediction id belongs to an OLDER image and must be dropped. Only keep the
    # remembered prediction id when no new image was uploaded.
    current_prediction_id = None if latest_image_s3_key else state.get("latest_prediction_id")

    # When a detection from an earlier request already exists and the user did
    # NOT upload a new image, give the model an explicit, in-context signal so
    # its tool choice is deterministic: it must reuse the existing detection via
    # show_annotated_image instead of redundantly re-running detect_objects.
    if current_prediction_id and latest_image_s3_key is None:
        lc_messages.append(
            SystemMessage(
                content=(
                    "A previous object detection already exists for this conversation. "
                    "If the user asks to see the annotated image, call show_annotated_image "
                    "directly. Do NOT call detect_objects again; no new image was uploaded."
                )
            )
        )

    token_image = _current_image_s3_key.set(current_image_s3_key)
    token_image_id = _current_image_id.set(current_image_id)
    # The original image key of the current flow: the freshly uploaded key, or
    # the one carried over from an earlier request. Never derived from
    # latest_image_s3_key, so "detect the original image" resolves to the true
    # original even after image-processing.
    token_original = _original_image_s3_key.set(current_original_image_s3_key)
    token_processed = _latest_processed_key.set(None)
    token_prediction = _latest_prediction_uid.set(current_prediction_id)


    try:
        result = run_agent(lc_messages)
        # Remember this conversation's image flow on the backend, keyed by
        # chat_id, so the next request resolves the same S3 keys without the
        # frontend ever seeing them.
        _chat_state[chat_id] = {
            "latest_image_s3_key": result["latest_image_s3_key"],
            "latest_image_id": result["latest_image_id"],
            "original_image_s3_key": result["original_image_s3_key"],
            "latest_prediction_id": result["prediction_id"],
        }
        return ChatResponse(
            response=result["response"],
            prediction_id=result["prediction_id"],
            annotated_image=result["annotated_image"],
            processed_image=result["processed_image"],
            agent_loop_time_s=result["agent_loop_time_s"],
            iterations=result["iterations"],
            tools_called=result["tools_called"],
            context_limit_exceeded=result["context_limit_exceeded"],
            tokens_used=TokensUsed(**result["tokens_used"]),
            image_url=result["image_url"],
        )
    finally:
        _current_image_s3_key.reset(token_image)
        _current_image_id.reset(token_image_id)
        _original_image_s3_key.reset(token_original)
        _latest_processed_key.reset(token_processed)
        _latest_prediction_uid.reset(token_prediction)
        

@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
