import os


ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
APP_VERSION = os.getenv("APP_VERSION", "0.3.0")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.3")
XAI_SUMMARY_MODEL = os.getenv("XAI_SUMMARY_MODEL", XAI_MODEL)
MAX_RESPONSE_TOKENS = max(1, int(os.getenv("MAX_RESPONSE_TOKENS", "512")))
MAX_USER_INPUT_TOKENS = max(1, int(os.getenv("MAX_USER_INPUT_TOKENS", "230")))
MAX_ATTACHED_FILE_LENGTH = max(1, int(os.getenv("MAX_ATTACHED_FILE_LENGTH", "230")))
MAX_ATTACHED_FILE_BYTES = max(1, int(os.getenv("MAX_ATTACHED_FILE_BYTES", str(5 * 1024 * 1024))))
MAX_FILE_CONTENT_SIZE = max(1, int(os.getenv("MAX_FILE_CONTENT_SIZE", "230")))
MAX_FILE_DESCRIPTION_TOKENS = max(1, int(os.getenv("MAX_FILE_DESCRIPTION_TOKENS", "64")))
MAX_PARALLEL_FILE_GENERATIONS = max(1, int(os.getenv("MAX_PARALLEL_FILE_GENERATIONS", "4")))
GENERATED_FILES_DIRECTORY = os.getenv("GENERATED_FILES_DIRECTORY", "/app/generated_files")
SUMMARY_MAX_TOKENS = max(1, int(os.getenv("SUMMARY_MAX_TOKENS", str(MAX_RESPONSE_TOKENS))))
SUMMARY_TRIGGER_RESPONSE_MULTIPLIER = max(1, int(os.getenv("SUMMARY_TRIGGER_RESPONSE_MULTIPLIER", "7")))
KEEP_RECENT_RESPONSE_MULTIPLIER = max(1, int(os.getenv("KEEP_RECENT_RESPONSE_MULTIPLIER", "3")))
SUMMARY_CHUNK_RESPONSE_MULTIPLIER = max(1, int(os.getenv("SUMMARY_CHUNK_RESPONSE_MULTIPLIER", "5")))
SUMMARY_CONTEXT_SUMMARY_MULTIPLIER = max(1, int(os.getenv("SUMMARY_CONTEXT_SUMMARY_MULTIPLIER", "3")))
MAX_CONTEXT_EXTRA_RESPONSE_MULTIPLIER = max(0, int(os.getenv("MAX_CONTEXT_EXTRA_RESPONSE_MULTIPLIER", "2")))



# Summarization becomes eligible when the older unsummarized conversation 
# reaches approximately SUMMARY_TRIGGER_TOKENS estimated tokens. 
# This counts raw messages that have not yet been included in a summary, 
# including user and assistant messages. Counts all unsummarized 
# raw tokens, including the protected KEEP_RECENT_TOKENS
SUMMARY_TRIGGER_TOKENS = MAX_RESPONSE_TOKENS * SUMMARY_TRIGGER_RESPONSE_MULTIPLIER
# Controls which messages are protected from summarization.
KEEP_RECENT_TOKENS = MAX_RESPONSE_TOKENS * KEEP_RECENT_RESPONSE_MULTIPLIER
# Maximum message range sent to the summarization model.
SUMMARY_CHUNK_MAX_TOKENS = MAX_RESPONSE_TOKENS * SUMMARY_CHUNK_RESPONSE_MULTIPLIER
# Maximum combined size of attached summary segments.
SUMMARY_CONTEXT_MAX_TOKENS = SUMMARY_MAX_TOKENS * SUMMARY_CONTEXT_SUMMARY_MULTIPLIER
# System prompt
# + attached summaries
# + raw messages
# + current message
# + MAX_RESPONSE_TOKENS #### for extra gap + MAX_RESPONSE_TOKENS
# ≤ MAX_CONTEXT_TOKENS
MAX_CONTEXT_TOKENS = SUMMARY_CONTEXT_MAX_TOKENS + SUMMARY_TRIGGER_TOKENS + MAX_USER_INPUT_TOKENS + MAX_RESPONSE_TOKENS + MAX_RESPONSE_TOKENS


MAX_SUMMARY_PASSES = max(1, int(os.getenv("MAX_SUMMARY_PASSES", "3")))
SUMMARY_WORKER_POLL_SECONDS = max(0.1, float(os.getenv("SUMMARY_WORKER_POLL_SECONDS", "1.0")))
SUMMARY_WORKER_METRICS_PORT = max(1, int(os.getenv("SUMMARY_WORKER_METRICS_PORT", "9101")))
