import base64
import io
import json
import logging
import os
import time
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
from langchain_core.tools import tool
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
YOLO_PUBLIC_URL = os.getenv("YOLO_PUBLIC_URL", YOLO_SERVICE_URL)
MODEL = os.environ.get("MODEL")

# Text-only models
ALLOWED_MODELS = {
    "openai:gpt-5.4-mini",
    "anthropic:claude-haiku-4-5",
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
    "- show_annotated_image needs a prior detection, so if none has run yet in this turn, "
    "call detect_objects first and then show_annotated_image."
)

_current_image_b64: ContextVar[Optional[str]] = ContextVar("current_image_b64", default=None)
_latest_prediction_uid: ContextVar[Optional[str]] = ContextVar("latest_prediction_uid", default=None)



@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    image_b64 = _current_image_b64.get()

    if not image_b64:
        return json.dumps({"error": "No image was provided by the user."})

    image_bytes = base64.b64decode(image_b64)

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            files={"file": ("image.jpg", io.BytesIO(image_bytes), "image/jpeg")},
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

llm = init_chat_model(MODEL, temperature=0)
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
    prediction_uid = None
    tools_called: list[str] = []
    iterations = 0
    context_limit_exceeded = False
    answer = "Agent stopped: maximum iterations reached."

    while iterations < max_iterations:
        iterations += 1

        response: AIMessage = llm_with_tools.invoke(messages)
        messages.append(response)

        # No tool calls, the model produced its final answer
        if not response.tool_calls:
            answer = response.content
            break

        # Execute every tool the model requested
        for tool_call in response.tool_calls:
            tool_fn = TOOLS[tool_call["name"]]
            tool_result = tool_fn.invoke(tool_call)
            messages.append(tool_result)
            tools_called.append(tool_call["name"])

            if tool_call["name"] == "detect_objects":
                # Store the UID in THIS context so a later show_annotated_image
                # call (which runs in a child context) can read it.
                tool_data = json.loads(tool_result.content)
                uid = tool_data.get("prediction_uid")
                if uid:
                    prediction_uid = uid
                    _latest_prediction_uid.set(uid)

            if tool_call["name"] == "detect_objects":
                # Store the UID in THIS context so a later show_annotated_image
                # call (which runs in a child context) can read it.
                tool_data = json.loads(tool_result.content)
                prediction_uid = tool_data.get("prediction_uid")
                if prediction_uid:
                    _latest_prediction_uid.set(prediction_uid)

            if tool_call["name"] == "show_annotated_image":
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


class ChatRequest(BaseModel):
    messages: list[ChatMessage]         # full conversation thread, oldest first


class ChatResponse(BaseModel):
    response: str
    prediction_id: str | None = None
    annotated_image: str | None = None
    agent_loop_time_s: float
    iterations: int
    tools_called: list[str]
    context_limit_exceeded: bool
    # Kept for backward compatibility with existing frontend clients.
    image_url: str | None = None


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    lc_messages = []
    latest_image = None

    for msg in request.messages:
        if msg.role == "user":
            if msg.image_base64:
                latest_image = msg.image_base64          # saved for detect_objects tool
                content = msg.content + "\n[An image was uploaded. Use existing tools to analyze it according to user instructions.]"
            else:
                content = msg.content
            lc_messages.append(HumanMessage(content=content))
        else:
            lc_messages.append(AIMessage(content=msg.content))

    token_image = _current_image_b64.set(latest_image)
    token_prediction = _latest_prediction_uid.set(None)
    

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
            image_url=result["image_url"],
        )
    finally:
        _current_image_b64.reset(token_image)
        _latest_prediction_uid.reset(token_prediction)
        

@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
