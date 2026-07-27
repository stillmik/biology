import logging
import os


from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langsmith.wrappers import wrap_openai
from openai import OpenAI


from .core.clients import close_xai_client, set_xai_client
from .core.config import APP_VERSION, MAX_CONTEXT_TOKENS, SUMMARY_TRIGGER_TOKENS, XAI_MODEL, XAI_SUMMARY_MODEL
from .core.observability import RequestObservabilityMiddleware, configure_logging, langsmith_tracing_extra, log_event, metrics_response
from .infrastructure.database import initialize_database_from_db
from .routers import chat, conversations, messages, users


load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    api_key = os.getenv("XAI_API_KEY")

    if not api_key:
        raise RuntimeError("XAI_API_KEY is not configured")

    log_event(logger, logging.INFO, "application_starting", model=XAI_MODEL, summary_model=XAI_SUMMARY_MODEL, summary_trigger_tokens=SUMMARY_TRIGGER_TOKENS, max_context_tokens=MAX_CONTEXT_TOKENS)
    initialize_database_from_db()
    log_event(logger, logging.INFO, "database_ready")
    set_xai_client(wrap_openai(OpenAI(api_key=api_key, base_url="https://api.x.ai/v1"), tracing_extra=langsmith_tracing_extra()))
    log_event(logger, logging.INFO, "application_ready")

    try:
        yield
    finally:
        log_event(logger, logging.INFO, "application_stopping")
        close_xai_client()


app = FastAPI(title="Biology Chat API", version=APP_VERSION, lifespan=lifespan)


app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:18080", "http://127.0.0.1:18080"], allow_credentials=False, allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["*"])


app.add_middleware(RequestObservabilityMiddleware)


app.include_router(users.router)
app.include_router(conversations.router)
app.include_router(chat.router)
app.include_router(messages.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return metrics_response()
