from prometheus_client import Counter, Histogram

AGENT_CHAT_REQUESTS_TOTAL = Counter(
    "agent_chat_requests_total",
    "Total number of /chat requests, by outcome.",
    ["status"],
)

AGENT_CHAT_REQUEST_DURATION_SECONDS = Histogram(
    "agent_chat_request_duration_seconds",
    "Duration of /chat requests in seconds.",
)

AGENT_INPUT_TOKENS_TOTAL = Counter(
    "agent_input_tokens_total",
    "Total number of LLM input tokens consumed across /chat requests.",
)

AGENT_OUTPUT_TOKENS_TOTAL = Counter(
    "agent_output_tokens_total",
    "Total number of LLM output tokens produced across /chat requests.",
)
