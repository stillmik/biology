# Biology Chat

A frontend/backend microbiology chat application for learning LangGraph incrementally.

The API saves the user message, runs an explicit LangGraph workflow, loads non-overlapping summary segments plus the complete raw tail after the newest segment, decides whether another segment is needed, and calls xAI's `grok-4.3`. Users, conversations, messages, and summary segments are stored in PostgreSQL.

The application also includes a reusable, per-user PDF library. Text-based PDFs
are extracted page by page, indexed with local BGE embeddings and PostgreSQL
full-text search, and answered with page-level citations. PDFs with at most 230
extracted tokens use a compact basic path. PDFs above 230 tokens use a
hierarchical document-analysis LangGraph with evidence, packet, section,
major-section, and root nodes as appropriate for their page count.

The application includes production-style observability:

- LangSmith traces chatbot runs, graph nodes, model calls, streaming, and summarization.
- Prometheus records bounded application metrics and evaluates alert rules.
- Loki stores structured JSON logs collected by Grafana Alloy.
- Grafana provisions overview, LangGraph, streaming, and PostgreSQL dashboards.

## Run

```powershell
docker compose up --build
```

Copy `.env.example` to `.env` and set `XAI_API_KEY` before starting the app. Set `LANGSMITH_API_KEY`, `OBSERVABILITY_HASH_SALT`, and a strong `GRAFANA_ADMIN_PASSWORD` to enable the complete observability setup.

Chat context includes the newest summary segments that fit `SUMMARY_CONTEXT_MAX_TOKENS`, in chronological order, plus every message after the newest segment checkpoint. Context and summary budgets are configurable through `.env`. Token counts use a conservative approximate character-based estimate because the xAI tokenizer is not bundled locally.

Open http://localhost:18080.

The API is available at http://localhost:18000 and its health check is at
http://localhost:18000/health.

Grafana is available at http://localhost:13000 and Prometheus at http://localhost:19090.

See [docs/observability.md](docs/observability.md) for tracing, dashboards, logs, alerts, summarization verification, free-tier controls, and troubleshooting.

## PDF analysis

PDF processing and document-grounded answers run in the `document-worker` and
`answer-worker` services. Original PDFs, embedding-model files, and PostgreSQL
data use separate persistent Docker volumes. PostgreSQL runs the pgvector image
and creates cosine HNSW, full-text GIN, and ownership/status indexes during
startup.

The document budgets are independent from all existing chat and conversation
summary settings:

| Environment variable | Default |
|---|---:|
| `DEEP_PDF_ANALYSIS_TRIGGER_TOKENS` | `230` |
| `MAX_DOCUMENT_EXTRACTED_TOKENS` | `200000` |
| `MAX_DOCUMENT_MODEL_INPUT_TOKENS` | `24000` |
| `MAX_DOCUMENT_EVIDENCE_TOKENS` | `18000` |
| `MAX_DOCUMENT_PACKET_SUMMARY_TOKENS` | `350` |
| `MAX_DOCUMENT_SECTION_SUMMARY_TOKENS` | `700` |
| `MAX_DOCUMENT_ROOT_SUMMARY_TOKENS` | `1200` |
| `MAX_DOCUMENT_ANSWER_TOKENS` | `2000` |
| `MAX_DOCUMENT_VERIFICATION_TOKENS` | `500` |

The new endpoints cover document upload, listing, status, original-file access,
retry, deletion, conversation attachment, and answer-job polling under
`/api/documents`, `/api/users/{user_id}/documents`,
`/api/conversations/{conversation_id}/documents`, and `/api/answer-jobs`.
The original `/api/chat/stream-with-file` endpoint remains compatible: TXT uses
the legacy direct attachment behavior, while PDF creates or reuses a library
document and queues its grounded answer.

Only extractable PDFs are supported. OCR, scanned pages, image interpretation,
and TXT hierarchy analysis are intentionally outside this implementation.

## Project layout

- `backend/` — FastAPI and LangGraph service.
- `frontend/` — static chat UI served by Nginx.
- `observability/` — Prometheus, Loki, Alloy, and Grafana configuration.
- `docs/` — operational guides.
- `compose.yaml` — local multi-container environment.
