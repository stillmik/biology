# Biology Chat

A minimal frontend/backend chat app for learning LangGraph incrementally.

The current backend contains one LangGraph node that sends the user's message
to xAI's `grok-4.3` model. Users and conversation history are stored in
PostgreSQL. It has no biology instructions, tools, or retrieval system yet.

## Run

```powershell
docker compose up --build
```

Copy `.env.example` to `.env` and set `XAI_API_KEY` before starting the app.

Open http://localhost:18080.

The API is available at http://localhost:18000 and its health check is at
http://localhost:18000/health.

## Project layout

- `backend/` — FastAPI service.
- `frontend/` — static chat UI served by Nginx.
- `compose.yaml` — local multi-container environment.
