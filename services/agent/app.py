import base64
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
from pydantic import BaseModel

from s3 import build_object_key, safe_image_name, upload_image

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
    "You are an AI vision assistant. You help users understand and analyze images.\n"
    "- Use the detect_objects tool to analyze an image and identify the objects in it.\n"
    "- When show_annotated_image is used, do NOT include the image URL in the text response.\n"
    "- The frontend will display the image automatically.\n"
    "- Mention that the annotated image is attached, but never print the URL.\n"
    "annotated image (the image with bounding boxes). It returns the picture with boxes drawn on it.\n"
    "- show_annotated_image requires a prior detection. If the conversation shows that a detection "
    "already exists, call show_annotated_image directly and do NOT re-run detect_objects. "
    "Only call detect_objects first when no prior detection exists yet (e.g. a newly uploaded image "
    "that has not been analyzed).\n"
    "When the user asks to analyze, detect, identify, or describe the image, call only detect_objects.\n"
    "Do not call show_annotated_image unless the user explicitly asks to see the annotated image, bounding boxes, marked image, or image with boxes."
)

_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)
_latest_prediction_uid: ContextVar[Optional[str]] = ContextVar("latest_prediction_uid", default=None)



@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    image_s3_key = _current_image_s3_key.get()

    if not image_s3_key:
        return json.dumps({"error": "No image was provided by the user."})

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            json={"image_s3_key": image_s3_key},
        )
        response.raise_for_status()

    data = response.json()
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


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects,
    show_annotated_image.name: show_annotated_image,
}

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

# Validate that the selected model supports tool calling before starting up.
# The model profile exposes its declared capabilities; if tool calling is not
# supported the agent cannot work, so fail fast with a clear startup error.

llm_with_tools = llm.bind_tools(list(TOOLS.values()))


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
    return base64.b64encode(response.content).decode("ascii")


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
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history
    image_url = None
    annotated_image = None
    # Seed from the context var, which holds request.latest_prediction_id (set
    # in chat() before this runs). This lets a follow-up "show annotated image"
    # request fetch the image for a detection that ran in an EARLIER request,
    # where detect_objects does not run again. It also makes the returned
    # prediction_id round-trip so the client keeps a valid id.
    prediction_uid = _latest_prediction_uid.get()
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

            if tool_name not in TOOLS:
                messages.append(
                    ToolMessage(
                        content=json.dumps({"error": f"Unknown tool: {tool_name}"}),
                        tool_call_id=tool_call["id"],
                    )
                )
                continue

            tool_fn = TOOLS[tool_name]
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
                    _latest_prediction_uid.set(uid)


            if tool_name == "show_annotated_image":
                tool_data = json.loads(tool_result.content)
                image_url = tool_data.get("image_url") or image_url
                # Fetch the annotated image bytes from YOLO and base64-encode
                # them here, OUTSIDE the LLM message flow, so the model never
                # sees image data (text-only architecture constraint).
                annotated_image = _fetch_annotated_image_b64(prediction_uid) or annotated_image
    else:
        # The while loop finished without `break`, meaning we hit max_iterations.
        context_limit_exceeded = True

    agent_loop_time_s = round(time.perf_counter() - start_time, 3)

    return {
        "response": answer,
        "image_url": image_url,
        "annotated_image": annotated_image,
        "prediction_id": prediction_uid,
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
    latest_prediction_id: Optional[str] = None  # prediction_id from a prior response, if any


class TokensUsed(BaseModel):
    input: int
    output: int
    total: int


class ChatResponse(BaseModel):
    response: str
    prediction_id: str | None = None
    annotated_image: str | None = None
    agent_loop_time_s: float
    iterations: int
    tools_called: list[str]
    context_limit_exceeded: bool
    tokens_used: TokensUsed
    # Kept for backward compatibility with existing frontend clients.
    image_url: str | None = None


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    chat_id = request.chat_id

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
    latest_user_msg = next(
        (m for m in reversed(request.messages) if m.role == "user"), None
    )
    if latest_user_msg and latest_user_msg.image_base64:
        # Each new image gets its own prediction_id under the stable chat_id.
        prediction_id = str(uuid.uuid4())
        image_bytes = base64.b64decode(latest_user_msg.image_base64)
        # Preserve the real uploaded filename in the key; if the client did not
        # send one, fall back to "<prediction_id>.jpg".
        image_name = safe_image_name(
            latest_user_msg.image_filename, fallback=f"{prediction_id}.jpg"
        )
        latest_image_s3_key = build_object_key(
            chat_id, prediction_id, "original", image_name
        )
        upload_image(latest_image_s3_key, image_bytes)

    # When a detection from an earlier request already exists and the user did
    # NOT upload a new image, give the model an explicit, in-context signal so
    # its tool choice is deterministic: it must reuse the existing detection via
    # show_annotated_image instead of redundantly re-running detect_objects.
    if request.latest_prediction_id and latest_image_s3_key is None:
        lc_messages.append(
            SystemMessage(
                content=(
                    "A previous object detection already exists for this conversation. "
                    "If the user asks to see the annotated image, call show_annotated_image "
                    "directly. Do NOT call detect_objects again; no new image was uploaded."
                )
            )
        )

    token_image = _current_image_s3_key.set(latest_image_s3_key)
    token_prediction = _latest_prediction_uid.set(request.latest_prediction_id)
    

    try:
        result = run_agent(lc_messages)
        return ChatResponse(
            response=result["response"],
            prediction_id=result["prediction_id"],
            annotated_image=result["annotated_image"],
            agent_loop_time_s=result["agent_loop_time_s"],
            iterations=result["iterations"],
            tools_called=result["tools_called"],
            context_limit_exceeded=result["context_limit_exceeded"],
            tokens_used=TokensUsed(**result["tokens_used"]),
            image_url=result["image_url"],
        )
    finally:
        _current_image_s3_key.reset(token_image)
        _latest_prediction_uid.reset(token_prediction)
        

@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
