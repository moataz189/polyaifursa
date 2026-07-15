export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  image_base64?: string;
  image_filename?: string;
  image_url?: string;
  annotated_image?: string | null;
  processed_image?: string | null;
}
