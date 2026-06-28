import type { ChatMessage } from "./types";

const AGENT_URL = process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8000";

export interface SendMessageResult {
  response: string;
  imageUrl: string | null;
  annotatedImage: string | null;
}

export async function sendMessage(
  chatId: string,
  messages: ChatMessage[]
): Promise<SendMessageResult> {
  const res = await fetch(`${AGENT_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, messages }),
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
  };
}
