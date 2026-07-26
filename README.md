# Biology Chat

A frontend/backend microbiology chat application for learning LangGraph incrementally.

The API saves the user message, runs an explicit LangGraph workflow, loads the complete unsummarized context after the latest summary checkpoint, decides whether another rolling summary is needed, and calls xAI's `grok-4.3`. Users, conversations, messages, and summaries are stored in PostgreSQL.

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

Chat context includes the latest rolling summary plus every message after its checkpoint. Context and summary budgets are configurable through `.env`. Token counts use a conservative approximate character-based estimate because the xAI tokenizer is not bundled locally.

Open http://localhost:18080.

The API is available at http://localhost:18000 and its health check is at
http://localhost:18000/health.

Grafana is available at http://localhost:13000 and Prometheus at http://localhost:19090.

See [docs/observability.md](docs/observability.md) for tracing, dashboards, logs, alerts, summarization verification, free-tier controls, and troubleshooting.

## Project layout

- `backend/` — FastAPI and LangGraph service.
- `frontend/` — static chat UI served by Nginx.
- `observability/` — Prometheus, Loki, Alloy, and Grafana configuration.
- `docs/` — operational guides.
- `compose.yaml` — local multi-container environment.
