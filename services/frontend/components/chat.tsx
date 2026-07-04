"use client";

import { useState, useRef, useEffect } from "react";
import { SendHorizontal, ImagePlus, X } from "lucide-react";
import { toast } from "sonner";
import { sendMessage } from "@/lib/api";
import type { ChatMessage } from "@/lib/types";
import MessageBubble from "./message-bubble";

export default function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [imageB64, setImageB64] = useState<string | null>(null);
  const [imageFilename, setImageFilename] = useState<string | null>(null);
  const [imagePreview, setImagePreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Generated once when the chat starts; sent with every /chat request so the
  // backend groups all images of this conversation under a stable chat_id.
 function generateChatId() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }

  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

  // Most recent prediction id returned by the backend. Sent on the next
  // request so a later "show annotated image" can find the prior detection.
  const latestPredictionIdRef = useRef<string | null>(null);
  // Latest usable image S3 key (uploaded or produced by a processing tool).
  // Sent on the next request so follow-ups operate on the same image. Not
  // shown to the user.
  const latestImageS3KeyRef = useRef<string | null>(null);
  // image_id of the current image flow. Distinct from the prediction id. Sent
  // on the next request so follow-ups stay within the same image flow.
  const latestImageIdRef = useRef<string | null>(null);
  // S3 key of the ORIGINAL uploaded image. Stays fixed across processing so
  // "detect the original image" resolves correctly. Sent on the next request.
  const originalImageS3KeyRef = useRef<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const chatIdRef = useRef<string>(generateChatId());

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      setImageB64(result.split(",")[1]);
      setImageFilename(file.name);
      setImagePreview(result);
    };
    reader.readAsDataURL(file);
    e.target.value = "";
  }

  function handleTextareaChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    e.target.style.height = "auto";
    e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim() && !imageB64) return;

    const userMessage: ChatMessage = {
      role: "user",
      content: input.trim() || "What's in this image?",
      ...(imageB64 ? { image_base64: imageB64 } : {}),
      ...(imageB64 && imageFilename ? { image_filename: imageFilename } : {}),
    };

    // A newly attached image has no detection yet, so drop the old prediction
    // id (and image key) before sending. The backend also resets these, but
    // clearing here keeps the client from carrying an older image's state.
    if (imageB64) {
      latestPredictionIdRef.current = null;
      latestImageS3KeyRef.current = null;
      latestImageIdRef.current = null;
      originalImageS3KeyRef.current = null;
    }

    const next = [...messages, userMessage];
    setMessages(next);
    setInput("");
    setImageB64(null);
    setImageFilename(null);
    setImagePreview(null);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    setLoading(true);

    try {
      const { response, imageUrl, annotatedImage, processedImage, predictionId, latestImageS3Key, latestImageId, originalImageS3Key } =
        await sendMessage(
          chatIdRef.current,
          next,
          latestPredictionIdRef.current,
          latestImageS3KeyRef.current,
          latestImageIdRef.current,
          originalImageS3KeyRef.current
        );
      // Always store the newest state from the response, even when it is null
      // (e.g. a processed image resets the prediction id), so future requests
      // never reuse an older image's prediction/key.
      latestPredictionIdRef.current = predictionId;
      latestImageS3KeyRef.current = latestImageS3Key;
      latestImageIdRef.current = latestImageId;
      originalImageS3KeyRef.current = originalImageS3Key;
      setMessages([
        ...next,
        {
          role: "assistant",
          content: response,
          ...(imageUrl ? { image_url: imageUrl } : {}),
          ...(annotatedImage ? { annotated_image: annotatedImage } : {}),
          ...(processedImage ? { processed_image: processedImage } : {}),
        },
      ]);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e as unknown as React.FormEvent);
    }
  }

  return (
    <div className="flex flex-col h-screen max-w-3xl mx-auto">
      {/* Header */}
      <div className="border-b px-6 py-4 shrink-0 bg-gradient-to-r from-background to-muted/30">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
          <h1 className="text-lg font-semibold tracking-tight">Vision Agent</h1>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-4">
        {messages.length === 0 && (
          <p className="text-center text-muted-foreground mt-20">
            Send a message or upload an image to get started.
          </p>
        )}
        {messages.map((msg, i) => (
          <MessageBubble key={i} message={msg} />
        ))}
        {loading && (
          <div className="flex gap-1 pl-1">
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="w-2 h-2 bg-primary/60 rounded-full animate-bounce"
                style={{ animationDelay: `${i * 0.15}s` }}
              />
            ))}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="border-t px-6 py-4 shrink-0">
        {imagePreview && (
          <div className="mb-3 relative inline-block">
            <img
              src={imagePreview}
              alt="preview"
              className="h-20 w-20 rounded-lg border object-cover"
            />
            <button
              onClick={() => {
                setImageB64(null);
                setImageFilename(null);
                setImagePreview(null);
              }}
              className="absolute -top-2 -right-2 bg-background border rounded-full p-0.5 hover:bg-muted"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
        <form onSubmit={handleSubmit} className="flex items-end gap-2">
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={handleFileChange}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title="Upload image"
            className="p-2 rounded-lg hover:bg-muted text-muted-foreground transition-colors shrink-0"
          >
            <ImagePlus className="w-5 h-5" />
          </button>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleTextareaChange}
            onKeyDown={handleKeyDown}
            placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
            rows={1}
            className="flex-1 resize-none rounded-xl border bg-muted/40 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-primary shadow-sm overflow-hidden"
          />
          <button
            type="submit"
            disabled={loading || (!input.trim() && !imageB64)}
            className="p-2.5 rounded-xl bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed transition-all shadow-sm shrink-0"
          >
            <SendHorizontal className="w-5 h-5" />
          </button>
        </form>
      </div>
    </div>
  );
}
