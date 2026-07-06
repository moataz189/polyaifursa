from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.responses import FileResponse, Response
from polars import datetime
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ultralytics import YOLO
from PIL import Image
import logging
import os
import uuid
import shutil
import time
import signal
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone
from s3 import (
    download_image,
    upload_image,
    build_annotated_key,
)

from db import get_db, init_db
from models import PredictionSession, DetectionObject

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Disable GPU usage
import torch
torch.cuda.is_available = lambda: False
is_shutting_down = False
app = FastAPI()

# Expose /metrics endpoint with default process metrics + FastAPI HTTP metrics
Instrumentator().instrument(app).expose(app)
def handle_sigterm(_signum, _frame):
    global is_shutting_down
    is_shutting_down = True
    logging.info("Received SIGTERM. Shutting down gracefully...")
    # Perform cleanup: close DB connections, finish pending work, etc.
    logging.info("Cleanup done. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
# Confidence threshold for object detection (0.0 - 1.0).
# Detections below this score are discarded.
# Override with: export CONFIDENCE_THRESHOLD=0.7
_raw_threshold = os.environ.get("CONFIDENCE_THRESHOLD")
if _raw_threshold is not None:
    CONFIDENCE_THRESHOLD = float(_raw_threshold)
    logging.info("CONFIDENCE_THRESHOLD set to %s (from environment)", CONFIDENCE_THRESHOLD)
else:
    CONFIDENCE_THRESHOLD = 0.5
    logging.info("CONFIDENCE_THRESHOLD not set, using default: %s", CONFIDENCE_THRESHOLD)


UPLOAD_DIR = "uploads/original"
PREDICTED_DIR = "uploads/predicted"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PREDICTED_DIR, exist_ok=True)


class PredictRequest(BaseModel):
    image_s3_key: str


class PredictionResponse(BaseModel):
    prediction_uid: str
    detection_count: int
    labels: list[str]
    time_took: float
    predicted_image_s3_key: str


# Download the AI model (tiny model ~6MB)
model = YOLO("yolov8n.pt")
@app.post("/predict")
def predict(request: PredictRequest, db: Session = Depends(get_db)):
    start_time = time.time()
    image_s3_key = request.image_s3_key

    ext = os.path.splitext(image_s3_key)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png"]:
        raise HTTPException(status_code=400, detail="Only image files are supported")

    # Every detection gets its own fresh prediction uid. The uid is NOT derived
    # from the S3 image key, so detecting the same image twice never collides on
    # the prediction_sessions.uid UNIQUE constraint.
    uid = str(uuid.uuid4())
    original_path = os.path.join(UPLOAD_DIR, uid + ext)
    predicted_path = os.path.join(PREDICTED_DIR, uid + ".png")

    image_bytes = download_image(image_s3_key)
    with open(original_path, "wb") as f:
        f.write(image_bytes)

    results = model(original_path, device="cpu", conf=CONFIDENCE_THRESHOLD)

    annotated_frame = results[0].plot()
    annotated_image = Image.fromarray(annotated_frame)
    annotated_image.save(predicted_path)

    # The annotated image is stored under the per-prediction folder:
    #   <chat_id>/<image_id>/predictions/<prediction_uid>/annotated_<name>.png
    predicted_s3_key = build_annotated_key(image_s3_key, uid)
    with open(predicted_path, "rb") as f:
        upload_image(predicted_s3_key, f.read(), content_type="image/png")

    session = PredictionSession(
        uid=uid,
        timestamp=datetime.now(timezone.utc),
        original_image=image_s3_key,
        predicted_image=predicted_s3_key,
    )
    db.add(session)

    detected_labels = []
    for box in results[0].boxes:
        label_idx = int(box.cls[0].item())
        label = model.names[label_idx]
        score = float(box.conf[0])
        bbox = box.xyxy[0].tolist()

        detection = DetectionObject(
            prediction_uid=uid,
            label=label,
            score=score,
            box=str(bbox),
        )
        db.add(detection)
        detected_labels.append(label)

    db.commit()

    processing_time = round(time.time() - start_time, 2)

    return {
        "prediction_uid": uid,
        "detection_count": len(results[0].boxes),
        "labels": detected_labels,
        "time_took": processing_time,
        "predicted_image_s3_key": predicted_s3_key,
    }

@app.get("/prediction/{uid}")
def get_prediction_by_uid(uid: str, db: Session = Depends(get_db)):
    """
    Get prediction session by uid with all detected objects
    """
    session = db.get(PredictionSession, uid)
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")
    
    return {
        "uid": session.uid,
        "timestamp": session.timestamp.isoformat(),
        "original_image": session.original_image,
        "predicted_image": session.predicted_image,
        "detection_objects": [
            {
                "id": obj.id,
                "label": obj.label,
                "score": obj.score,
                "box": obj.box
            } for obj in session.detection_objects
        ]
    }


@app.get("/prediction/{uid}/image")
def get_prediction_image(uid: str, db: Session = Depends(get_db)):
    """
    Return the annotated image for a prediction, downloaded from S3 using
    the stored predicted-image object key.
    """
    session = db.get(PredictionSession, uid)
    if not session or not session.predicted_image:
        raise HTTPException(status_code=404, detail="Image not found")

    image_bytes = download_image(session.predicted_image)
    return Response(content=image_bytes, media_type="image/png")



@app.get("/health")
def health():
    """
    Health check endpoint
    """
    return {"status": "ok"}

@app.get("/check")
def check():
    """
    Check endpoint
    """
    return {"status": "ok"}

@app.get("/predictions/label/{label}")
def get_predictions_by_label(label: str, db: Session = Depends(get_db)):
    if not label.strip():
        raise HTTPException(
            status_code=400,
            detail="Label cannot be empty"
        )

    # Query sessions that have detections with this label
    sessions = (
        db.query(PredictionSession)
        .join(DetectionObject, PredictionSession.uid == DetectionObject.prediction_uid)
        .filter(DetectionObject.label == label)
        .all()
    )

    result = []
    for session in sessions:
        result.append({
            "uid": session.uid,
            "timestamp": session.timestamp.isoformat(),
            "detection_objects": [
                {
                    "id": obj.id,
                    "label": obj.label,
                    "score": obj.score,
                    "box": obj.box
                }
                for obj in session.detection_objects
                if obj.label == label
            ]
        })

    return result
    

@app.get("/predictions/score/{min_score}")
def get_predictions_by_score(min_score: float, db: Session = Depends(get_db)):
    if min_score < 0.0 or min_score > 1.0:
        raise HTTPException(
            status_code=400,
            detail="min_score must be between 0.0 and 1.0"
        )

    objects = (
        db.query(DetectionObject)
        .filter(DetectionObject.score >= min_score)
        .all()
    )

    return [
        {
            "id": obj.id,
            "prediction_uid": obj.prediction_uid,
            "label": obj.label,
            "score": obj.score,
            "box": obj.box
        }
        for obj in objects
    ]


@app.get("/predictions/recent")
def get_recent_predictions(db: Session = Depends(get_db)):
    """
    Get the 10 most recent prediction sessions
    """
    sessions = (
        db.query(PredictionSession)
        .order_by(PredictionSession.timestamp.desc())
        .limit(10)
        .all()
    )

    return [
        {
            "uid": session.uid,
            "timestamp": session.timestamp.isoformat(),
            "original_image": session.original_image,
            "predicted_image": session.predicted_image,
            "detection_objects": [
                {
                    "id": obj.id,
                    "label": obj.label,
                    "score": obj.score,
                    "box": obj.box
                }
                for obj in session.detection_objects
            ]
        }
        for session in sessions
    ]
@app.get("/image/{type}/{filename}")
def get_image(type: str, filename: str):

    if type == "original":
        image_path = os.path.join(UPLOAD_DIR, filename)

    elif type == "predicted":
        image_path = os.path.join(PREDICTED_DIR, filename)

    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid image type"
        )

    if not os.path.exists(image_path):
        raise HTTPException(
            status_code=404,
            detail="Image not found"
        )

    return FileResponse(image_path)

@app.get("/ready")
def ready():
    if is_shutting_down:
        raise HTTPException(status_code=503, detail="Service is shutting down")
    return {"status": "ready"}

if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    init_db()
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
