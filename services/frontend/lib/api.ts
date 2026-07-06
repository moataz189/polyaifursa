import type { ChatMessage } from "./types";

const AGENT_URL = process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8000";

export interface SendMessageResult {
  response: string;
  imageUrl: string | null;
  annotatedImage: string | null;
  processedImage: string | null;
  predictionId: string | null;
  latestImageS3Key: string | null;
  latestImageId: string | null;
  originalImageS3Key: string | null;
}

export async function sendMessage(
  chatId: string,
  messages: ChatMessage[],
  latestPredictionId: string | null,
  latestImageS3Key: string | null,
  latestImageId: string | null,
  originalImageS3Key: string | null
): Promise<SendMessageResult> {
  const res = await fetch(`${AGENT_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      messages,
      latest_prediction_id: latestPredictionId,
      latest_image_s3_key: latestImageS3Key,
      latest_image_id: latestImageId,
      original_image_s3_key: originalImageS3Key,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(text || res.statusText);
  }
  const data = await res.json();
  return {
    response: data.response as string,
    // The agent returns a relative path (e.g. "/image/<uid>").
    // Turn it into an absolute URL pointing at the agent service.
    imageUrl: data.image_url ?? null,
    // Base64-encoded annotated image (with bounding boxes), or null.
    annotatedImage: data.annotated_image ?? null,
    // Base64-encoded processed image (rotate/blur/flip/resize/crop/noise), or null.
    processedImage: data.processed_image ?? null,
    // Most recent prediction id, sent back on future requests so a later
    // "show annotated image" can find the previous detection.
    predictionId: data.prediction_id ?? null,
    // Latest usable image S3 key (uploaded or produced by a processing tool).
    // Sent back on future requests so follow-ups operate on the same image.
    // Never displayed to the user.
    latestImageS3Key: data.latest_image_s3_key ?? null,
    // image_id of the current image flow. Distinct from predictionId. Sent back
    // on future requests so follow-ups stay within the same image flow.
    latestImageId: data.latest_image_id ?? null,
    // S3 key of the ORIGINAL uploaded image. Stays fixed across processing so
    // "detect the original image" resolves correctly. Sent back on future
    // requests. Never displayed to the user.
    originalImageS3Key: data.original_image_s3_key ?? null,
  };
}
