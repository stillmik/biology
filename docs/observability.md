# Biology Chat observability

The application uses LangSmith for semantic LangGraph and model traces, Prometheus for low-cardinality metrics and alert rules, Loki for structured operational logs, Grafana for dashboards, and Alloy for Docker log collection.

No hosted Grafana service is used. Prometheus, Loki, Grafana, and Alloy run on the same Docker host as the application. LangSmith uses its free Developer account; verify the current allowance on the [official LangSmith pricing page](https://www.langchain.com/pricing).

## Responsibility map

| Question | Primary tool |
|---|---|
| What context did Grok receive? | LangSmith |
| Why was summarization triggered or skipped? | LangSmith |
| What did each graph node receive and return? | LangSmith |
| Is the API healthy overall? | Grafana and Prometheus |
| Which exception occurred? | Loki |
| Is streaming slow? | Grafana, Prometheus, then LangSmith |
| Is PostgreSQL failing? | Grafana and Loki |
| Did an edited message rebuild the timeline correctly? | LangSmith and mutation logs |

Prompts, summaries, and model responses belong in LangSmith. They must not be added to Loki events or Prometheus labels. API keys, cookies, authorization headers, database URLs, and other secrets must not be sent to any observability system.

The backend uses one LangSmith client with a client-side anonymizer for root traces, LangGraph callbacks, and wrapped xAI calls. It masks xAI, LangSmith, OpenAI-style, bearer-token, and PostgreSQL credential patterns before export. Keep this rule set updated when another credential format is introduced, and never treat automatic redaction as permission to deliberately place secrets in prompts.

## Configuration

Copy `.env.example` to `.env` and provide real values for these settings:

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=biology-chat
LANGSMITH_TRACING_SAMPLING_RATE=1.0
LANGSMITH_WORKSPACE_ID=
OBSERVABILITY_HASH_SALT=a-long-random-value
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=a-strong-password
```

`LANGSMITH_WORKSPACE_ID` is needed only when the API key can access multiple workspaces. `LANGSMITH_ORG_ID` and `LANGSMITH_PROJECT_ID` are optional; when supplied, trace IDs in Loki become direct LangSmith links from Grafana.

Generate a salt on PowerShell with:

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

The actual `.env` file is ignored by Git. Never commit it.

## Start the stack

Start the application and observability services:

```powershell
docker compose up --build -d
docker compose ps
```

Open:

- Chat: http://localhost:18080
- API health: http://localhost:18000/health
- Backend metrics: http://localhost:18000/metrics
- Grafana: http://localhost:13000
- Prometheus: http://localhost:19090
- Alloy status: http://localhost:12345

Loki is intentionally available only inside the Docker network.

The optional host exporters are intended for a Linux VPS:

```bash
docker compose --profile host-monitoring up -d node-exporter cadvisor
```

Docker Desktop may not expose Linux host filesystems and devices in the same way. Leave this profile disabled during ordinary Windows development.

## LangSmith traces

One normal message creates a `biology-chat_response` root trace. A streamed message creates `biology-chat_stream`, and an edited message creates `biology-message_regeneration`.

Each conversation is grouped using its PostgreSQL conversation ID as LangSmith `thread_id`. The root trace carries:

- hashed user ID;
- conversation and thread IDs;
- request ID;
- environment and application version;
- execution type and model;
- edited message ID and deleted-message count for regeneration.

The graph creates child runs in this order:

```text
START
  ↓
load_context
  ↓
needs_summary
  ├─ build_context ─────────────┐
  └─ summarize → reload_context ┘
                                ↓
                              grok
                                ↓
                               END
```

Multiple `summarize → reload_context → needs_summary` passes may occur until context is small enough or `MAX_SUMMARY_PASSES` is reached.

The wrapped xAI Responses API creates an LLM child run containing model input, streamed output, timing, invocation parameters, and provider-reported usage.

LangSmith exporting is fail-open. Missing credentials, quota exhaustion, or a temporary LangSmith failure must not stop database writes or chat responses.

## Verify summarization

Use a temporary development threshold so the summary branch is easy to trigger:

```env
SUMMARY_TRIGGER_TOKENS=300
KEEP_RECENT_TOKENS=100
SUMMARY_CHUNK_MAX_TOKENS=200
MAX_SUMMARY_PASSES=3
```

Rebuild the backend, create a fresh conversation, and send several detailed messages. Then:

1. Open the `biology-chat` LangSmith project.
2. Open **Threads** and select the thread whose `thread_id` equals the PostgreSQL conversation ID.
3. Open the latest trace.
4. Confirm `needs_summary` returns `summary_decision=summarize` with `context_above_trigger_threshold`.
5. Inspect `summarize` and compare the previous summary, selected older messages, generated replacement summary, and cursor.
6. Inspect `reload_context` and confirm only messages after the new summary cursor remain unsummarized.
7. Inspect `build_context` and confirm Grok receives one rolling summary plus every unsummarized message.
8. Check the **Biology Chat · LangGraph & Summarization** dashboard for matching branch, context, and reduction metrics.
9. Restore the normal thresholds after the test.

Useful PostgreSQL inspection:

```powershell
docker compose exec db psql -U biology -d biology_chat -c "SELECT id, conversation_id, token_count, covered_until_message_id, created_at FROM conversation_summaries ORDER BY id DESC LIMIT 10;"
```

The summary cursor must increase monotonically during ordinary chat. Editing an earlier message deliberately deletes summaries whose cursor covers that message.

## Grafana dashboards

Grafana provisions four dashboards automatically:

- **Biology Chat · Overview** for request health, errors, and application operations;
- **Biology Chat · LangGraph & Summarization** for graph paths, node latency, context size, and summary effectiveness;
- **Biology Chat · Model Streaming** for first-token latency, stream duration, output shape, and disconnects;
- **Biology Chat · PostgreSQL** for database latency, failures, connections, and advisory-lock waits.

Dashboard JSON lives in `observability/grafana/dashboards`. Edit those files rather than changing provisioned dashboards in the UI.

## Prometheus

Prometheus scrapes the backend every 15 seconds and stores seven days of data with a 2 GB maximum TSDB size.

Useful queries:

```promql
sum(rate(biology_http_requests_total[5m]))
histogram_quantile(0.95, sum by (le, endpoint) (rate(biology_http_request_duration_seconds_bucket[5m])))
sum by (branch) (increase(biology_langgraph_branches_total[1h]))
histogram_quantile(0.95, sum by (le, node) (rate(biology_langgraph_node_duration_seconds_bucket[10m])))
histogram_quantile(0.95, sum by (le) (rate(biology_model_first_token_seconds_bucket[10m])))
sum(increase(biology_langsmith_traces_attempted_total[30d]))
```

Identifiers and user-generated content are intentionally excluded from labels. Adding them would create unbounded cardinality and leak data.

Alert rules live in `observability/prometheus/alerts.yml`. They cover:

- backend availability and elevated HTTP errors;
- PostgreSQL failures;
- summary, context-budget, and graph-node failures;
- xAI failures and slow or broken streaming;
- LangSmith trace allowance warnings;
- optional Linux host disk and container-memory pressure.

Prometheus evaluates the rules and Grafana displays data-source-managed alerts. To send notifications, connect an existing free SMTP account through Grafana Alerting or add a self-hosted Alertmanager; no paid notification service is required.

## Loki

The backend emits one-line JSON records. Alloy discovers only containers labeled `logging=alloy` and sends their logs to Loki.

Useful LogQL:

```logql
{service="backend"} | json
{service="backend"} | json | level="ERROR"
{service="backend"} | json | event="summary_decision"
{service="backend"} | json | event=~"model_stream_failed|database_operation_failed"
{service="backend"} | json | conversation_id="42"
```

Every application log has a request ID. Logs created inside a LangSmith trace also contain `langsmith_trace_id` when the SDK exposes the active trace context.

Loki retains seven days of data. Docker itself rotates backend logs at 20 MB with five files and other application logs at 10 MB with three files.

## Free-tier protection

`biology_langsmith_traces_attempted_total` counts root traces attempted by each backend process. The alert rules estimate rolling 30-day usage at 4,000 and 4,500 attempts.

This metric resets when the backend process restarts, so LangSmith's Usage page remains the source of truth. If usage approaches the current free allowance, change:

```env
LANGSMITH_TRACING_SAMPLING_RATE=0.1
```

Then recreate the backend:

```powershell
docker compose up -d --force-recreate backend
```

Prometheus and Loki continue observing every request when LangSmith sampling is reduced or its quota is exhausted.

## Troubleshooting

Check container health:

```powershell
docker compose ps
docker compose logs --tail 100 backend prometheus loki alloy grafana
```

Check Prometheus targets at http://localhost:19090/targets. `node-exporter` and `cadvisor` are expected to be down unless the `host-monitoring` profile is enabled.

Validate Prometheus files:

```powershell
docker compose run --rm --no-deps prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose run --rm --no-deps prometheus promtool check rules /etc/prometheus/alerts.yml
```

If LangSmith has no traces, verify the API key, project name, workspace ID, and `LANGSMITH_TRACING=true`, then inspect backend logs for `langsmith_trace_start_failed` or `langsmith_trace_finish_failed`.

If Grafana has no logs, inspect Alloy at http://localhost:12345 and verify the application containers have the `logging=alloy` label.

## Retention and backups

Prometheus and Loki are operational diagnostics rather than primary application data. Their seven-day volumes may be discarded and recreated if corrupted.

Grafana dashboards and data-source definitions are stored in Git. The Grafana volume contains local users and UI settings; back it up only if those customizations matter.

PostgreSQL remains the authoritative data store and needs its own backup policy.
