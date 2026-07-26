import os


ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
APP_VERSION = os.getenv("APP_VERSION", "0.3.0")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.3")
XAI_SUMMARY_MODEL = os.getenv("XAI_SUMMARY_MODEL", XAI_MODEL)
SUMMARY_TRIGGER_TOKENS = max(1, int(os.getenv("SUMMARY_TRIGGER_TOKENS", "6000")))
KEEP_RECENT_TOKENS = max(1, int(os.getenv("KEEP_RECENT_TOKENS", "2500")))
SUMMARY_CHUNK_MAX_TOKENS = max(1, int(os.getenv("SUMMARY_CHUNK_MAX_TOKENS", "3500")))
SUMMARY_MAX_TOKENS = max(1, int(os.getenv("SUMMARY_MAX_TOKENS", "800")))
MAX_CONTEXT_TOKENS = max(1, int(os.getenv("MAX_CONTEXT_TOKENS", "12000")))
MAX_RESPONSE_TOKENS = max(1, int(os.getenv("MAX_RESPONSE_TOKENS", "512")))
MAX_SUMMARY_PASSES = max(1, int(os.getenv("MAX_SUMMARY_PASSES", "3")))
