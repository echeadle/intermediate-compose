---
module: 8
title: "Observability & Operations"
duration: "½ day (~3–4 hours)"
prerequisites: "Modules 1–7 complete"
---

# Module 8: Observability & Operations

## Introduction

A stack you can't observe is a stack you can't operate. When an agent run takes
45 seconds instead of 4, you need to know whether the bottleneck is the
embedding service, the vector search, the LLM call, or the MCP tool dispatch.
When your API starts returning 500s at 2am, you need logs that tell you why
without requiring a shell session.

Agent stacks are harder to observe than typical web services for two reasons.
First, a single user request triggers a cascade of internal calls — to the
embedding service, the vector store, the MCP server, the LLM — and you need
to trace that entire chain. Second, the work is often async: a task submitted
to the API might be processed by a worker 30 seconds later, in a different
container, with a different log context.

This module covers the full observability stack as Compose services: Prometheus
for metrics, Grafana for dashboards, Loki and Promtail for centralized logging,
and OpenTelemetry for distributed tracing across agent pipelines. It closes with
resource limits — the operational control that prevents a runaway embedding job
from starving every other service in your stack.

This is also the capstone module. By the end you have a complete,
production-shaped Compose stack that you can observe, debug, and operate.

---

## Learning Objectives

By the end of this module you will be able to:

- Instrument a FastAPI application with Prometheus metrics including custom
  agent-specific counters and histograms
- Provision Grafana dashboards as code alongside your Compose stack
- Collect and query logs from all services centrally using Loki and Promtail
- Add OpenTelemetry tracing to trace a request across the API, MCP server,
  and embedding service
- Set memory and CPU limits that protect critical services from noisy neighbors
- Use `docker compose watch` for file sync without bind mount overhead

---

## 1. Prometheus Metrics

### FastAPI instrumentation

The `prometheus-fastapi-instrumentator` library adds the `/metrics` endpoint
to any FastAPI app with two lines, exposing request counts, latencies, and
error rates per endpoint automatically.

```python
# src/myapp/main.py

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram, Gauge
import time

app = FastAPI(title="Argos Agent API")

# ── Auto-instrumentation (request count, latency, error rate per endpoint) ──
Instrumentator(
    should_group_status_codes=False,
    excluded_handlers=["/health", "/metrics"],
).instrument(app).expose(app, include_in_schema=False)


# ── Custom agent metrics ────────────────────────────────────────────────────

agent_tasks_total = Counter(
    "agent_tasks_total",
    "Total agent tasks submitted",
    labelnames=["status"],          # labels: submitted, completed, failed
)

agent_task_duration_seconds = Histogram(
    "agent_task_duration_seconds",
    "End-to-end agent task duration in seconds",
    buckets=[1, 5, 10, 30, 60, 120, 300],
    labelnames=["model"],
)

embed_request_duration_seconds = Histogram(
    "embed_request_duration_seconds",
    "Embedding service request duration",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    labelnames=["batch_size"],
)

vector_search_duration_seconds = Histogram(
    "vector_search_duration_seconds",
    "Vector search duration by backend",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0],
    labelnames=["backend"],
)

active_workers = Gauge(
    "active_workers",
    "Number of currently active agent workers",
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed",
    labelnames=["model", "direction"],   # direction: input, output
)
```

### Instrumenting agent operations

Wrap the slow paths with metric recording:

```python
# src/myapp/agents/research_agent.py

import time
from myapp.main import (
    agent_tasks_total,
    agent_task_duration_seconds,
    llm_tokens_total,
    active_workers,
)
from myapp.config import get_settings


async def run_agent_task(task: dict) -> dict:
    settings = get_settings()
    agent_tasks_total.labels(status="submitted").inc()
    active_workers.inc()
    start = time.monotonic()

    try:
        agent = get_research_agent()
        result = await agent.run(task["prompt"])

        duration = time.monotonic() - start
        agent_task_duration_seconds.labels(
            model=settings.ollama_model
        ).observe(duration)

        usage = result.usage()
        llm_tokens_total.labels(
            model=settings.ollama_model, direction="input"
        ).inc(usage.request_tokens or 0)
        llm_tokens_total.labels(
            model=settings.ollama_model, direction="output"
        ).inc(usage.response_tokens or 0)

        agent_tasks_total.labels(status="completed").inc()
        return {"result": result.data, "duration_seconds": duration}

    except Exception as e:
        agent_tasks_total.labels(status="failed").inc()
        raise

    finally:
        active_workers.dec()
```

### Prometheus configuration

```yaml
# monitoring/prometheus.yml

global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - /etc/prometheus/alerts/*.yml

scrape_configs:
  - job_name: api
    static_configs:
      - targets: ["api:8000"]
    metrics_path: /metrics

  - job_name: mcp-server
    static_configs:
      - targets: ["mcp-server:8002"]
    metrics_path: /metrics

  - job_name: embed
    static_configs:
      - targets: ["embed:8001"]
    metrics_path: /metrics

  - job_name: mongodb
    static_configs:
      - targets: ["mongo-exporter:9216"]

  - job_name: redis
    static_configs:
      - targets: ["redis-exporter:9121"]

  - job_name: node
    static_configs:
      - targets: ["node-exporter:9100"]
```

### Alerting rules

```yaml
# monitoring/alerts/agent.yml

groups:
  - name: agent_alerts
    rules:

      - alert: HighAgentFailureRate
        expr: |
          rate(agent_tasks_total{status="failed"}[5m])
          / rate(agent_tasks_total{status="submitted"}[5m]) > 0.1
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Agent failure rate above 10%"
          description: "{{ $value | humanizePercentage }} of tasks failing"

      - alert: SlowEmbedService
        expr: |
          histogram_quantile(0.95,
            rate(embed_request_duration_seconds_bucket[5m])
          ) > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Embedding service p95 latency above 5s"

      - alert: LLMTokenBudgetHigh
        expr: |
          rate(llm_tokens_total[1h]) > 10000
        for: 10m
        labels:
          severity: info
        annotations:
          summary: "High LLM token consumption rate"
```

---

## 2. Grafana Dashboards as Code

Grafana supports provisioning datasources and dashboards from config files
mounted as bind mounts. This means your dashboards live in git alongside your
code — no manual dashboard creation, no export/import dance.

### Directory structure

```
monitoring/
├── prometheus.yml
├── alerts/
│   └── agent.yml
└── grafana/
    ├── datasources/
    │   └── prometheus.yml
    │   └── loki.yml
    └── dashboards/
        ├── dashboards.yml          # tells Grafana where to find dashboards
        ├── agent-overview.json     # main agent dashboard
        └── infrastructure.json     # host and service health
```

### Datasource provisioning

```yaml
# monitoring/grafana/datasources/prometheus.yml

apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    url: http://prometheus:9090
    isDefault: true
    editable: false

  - name: Loki
    type: loki
    url: http://loki:3100
    editable: false
```

### Dashboard provisioning config

```yaml
# monitoring/grafana/dashboards/dashboards.yml

apiVersion: 1

providers:
  - name: default
    folder: Argos
    type: file
    options:
      path: /etc/grafana/provisioning/dashboards
      foldersFromFilesStructure: true
```

### Agent overview dashboard (key panels)

Rather than paste a full Grafana JSON (several hundred lines), here are the
key panels to build manually or generate with `grafanalib`:

```
Row: Agent Health
  ├── Agent task rate (submitted/completed/failed) — time series
  ├── Active workers right now — stat panel
  ├── Task success rate last 1h — gauge (target: >95%)
  └── p50 / p95 / p99 task duration — time series

Row: LLM Usage
  ├── Tokens per minute (input vs output) — time series
  ├── Cumulative tokens today — stat panel
  └── Average tokens per task — time series

Row: Embedding Service
  ├── Embed request rate — time series
  ├── p95 embed latency — time series
  └── Embed error rate — time series

Row: Vector Search
  ├── Search latency by backend — time series
  └── Search request rate — time series

Row: Infrastructure
  ├── CPU usage by service — time series
  ├── Memory usage by service — time series
  └── MongoDB operation rate — time series
```

### Grafana in Compose

```yaml
services:
  grafana:
    image: grafana/grafana:10.4.0
    networks:
      - frontend
      - monitoring
    volumes:
      - grafana-data:/var/lib/grafana
      - ./monitoring/grafana/datasources:/etc/grafana/provisioning/datasources:ro
      - ./monitoring/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
    environment:
      GF_SECURITY_ADMIN_USER: ${GRAFANA_USER:-admin}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH: >
        /etc/grafana/provisioning/dashboards/agent-overview.json
    ports:
      - "127.0.0.1:3000:3000"
    profiles:
      - monitoring
    depends_on:
      - prometheus
```

---

## 3. Centralized Logging with Loki and Promtail

Rather than `docker compose logs -f`, centralized logging lets you query logs
from all services in one place, correlate them by time, and filter by labels —
critical when you're tracing a task that touches five services.

### How it works

```
Each container's stdout/stderr
    │
    ▼
Promtail (log collector — runs on every host)
    │  reads Docker container logs via /var/lib/docker/containers
    │  adds labels: container name, service name, compose project
    ▼
Loki (log aggregation backend)
    │
    ▼
Grafana (query and visualize via LogQL)
```

### Loki configuration

```yaml
# monitoring/loki-config.yml

auth_enabled: false

server:
  http_listen_port: 3100

ingester:
  lifecycler:
    ring:
      kvstore:
        store: inmemory
      replication_factor: 1

schema_config:
  configs:
    - from: 2024-01-01
      store: boltdb-shipper
      object_store: filesystem
      schema: v12
      index:
        prefix: index_
        period: 24h

storage_config:
  boltdb_shipper:
    active_index_directory: /loki/boltdb-shipper-active
    cache_location: /loki/boltdb-shipper-cache
    shared_store: filesystem
  filesystem:
    directory: /loki/chunks

limits_config:
  retention_period: 168h      # 7 days

compactor:
  working_directory: /loki/boltdb-shipper-compactor
  shared_store: filesystem
  retention_enabled: true
```

### Promtail configuration

```yaml
# monitoring/promtail-config.yml

server:
  http_listen_port: 9080

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: docker-containers
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
        refresh_interval: 5s
        filters:
          # Only collect logs from our Compose project
          - name: label
            values: ["com.docker.compose.project=argos"]

    relabel_configs:
      # Use the Compose service name as the "service" label in Loki
      - source_labels: [__meta_docker_container_label_com_docker_compose_service]
        target_label: service

      # Add the container name
      - source_labels: [__meta_docker_container_name]
        target_label: container

      # Add the Compose project name
      - source_labels: [__meta_docker_container_label_com_docker_compose_project]
        target_label: project

    pipeline_stages:
      # Parse JSON logs (if your app logs JSON)
      - json:
          expressions:
            level: level
            msg: message
            trace_id: trace_id      # preserve trace IDs for correlation

      - labels:
          level:
          trace_id:
```

### Loki and Promtail in Compose

```yaml
services:
  loki:
    image: grafana/loki:2.9.0
    networks:
      - monitoring
    volumes:
      - ./monitoring/loki-config.yml:/etc/loki/loki-config.yml:ro
      - loki-data:/loki
    command: -config.file=/etc/loki/loki-config.yml
    profiles:
      - monitoring
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1",
             "--spider", "http://localhost:3100/ready"]
      interval: 10s
      timeout: 5s
      retries: 5

  promtail:
    image: grafana/promtail:2.9.0
    networks:
      - monitoring
    volumes:
      - ./monitoring/promtail-config.yml:/etc/promtail/config.yml:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    command: -config.file=/etc/promtail/config.yml
    profiles:
      - monitoring
    depends_on:
      - loki

volumes:
  loki-data:
```

### Structured logging in Python

Promtail can parse JSON logs and extract fields as Loki labels. Configure your
app to emit structured logs:

```python
# src/myapp/logging_config.py

import logging
import json
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Emit structured JSON logs for Promtail/Loki ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        log = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
        }

        # Include trace ID if set on the record (added by middleware)
        if hasattr(record, "trace_id"):
            log["trace_id"] = record.trace_id

        # Include task ID for worker logs
        if hasattr(record, "task_id"):
            log["task_id"] = record.task_id

        # Include exception info if present
        if record.exc_info:
            log["exception"] = self.formatException(record.exc_info)

        return json.dumps(log)


def configure_logging(level: str = "info"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    logging.basicConfig(
        level=level.upper(),
        handlers=[handler],
        force=True,
    )
```

### Useful LogQL queries in Grafana

```logql
# All errors across the entire stack
{project="argos"} |= "error" | json | level="error"

# API logs for a specific trace ID
{service="api"} | json | trace_id="abc123def456"

# Agent task failures with context
{service="worker"} | json | message=~"Task .* failed"

# Slow embed requests (if timing is logged)
{service="embed"} | json
  | duration > 2s

# Everything from a 5-minute window around an incident
{project="argos"}
  [2024-01-15T14:25:00Z, 2024-01-15T14:30:00Z]
```

---

## 4. Distributed Tracing with OpenTelemetry

Metrics tell you *that* something is slow. Traces tell you *where* in the call
chain the time was spent. For an agent pipeline that calls five services, this
is the difference between "p95 task latency is 45s" and "the MCP server's
vector search is taking 38s of that 45s."

### OpenTelemetry setup

```python
# src/myapp/tracing.py

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.b3 import B3MultiFormat
from myapp.config import get_settings


def configure_tracing(app=None):
    settings = get_settings()

    exporter = OTLPSpanExporter(
        endpoint=f"http://{settings.otel_host}:{settings.otel_grpc_port}",
    )

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Propagate trace context using B3 headers (works across HTTP calls)
    set_global_textmap(B3MultiFormat())

    # Auto-instrument FastAPI, httpx, and Redis
    if app:
        FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()


# Get a tracer for manual spans
def get_tracer() -> trace.Tracer:
    return trace.get_tracer("myapp")
```

### Manual spans for agent operations

```python
# src/myapp/agents/research_agent.py

from myapp.tracing import get_tracer
from opentelemetry import trace

tracer = get_tracer()


async def run_agent_task(task: dict) -> dict:
    with tracer.start_as_current_span(
        "agent.run",
        attributes={
            "task.id": task["task_id"],
            "agent.model": get_settings().ollama_model,
        },
    ) as span:

        # Embedding span
        with tracer.start_as_current_span("embed.query"):
            query_embedding = await embed_texts([task["prompt"]])

        # Vector search span
        with tracer.start_as_current_span(
            "vector.search",
            attributes={"vector.backend": get_settings().vector_backend},
        ) as search_span:
            results = await vector_store.search(query_embedding[0])
            search_span.set_attribute("vector.results_count", len(results))

        # LLM inference span
        with tracer.start_as_current_span(
            "llm.inference",
            attributes={"llm.model": get_settings().ollama_model},
        ) as llm_span:
            agent = get_research_agent()
            result = await agent.run(task["prompt"])
            usage = result.usage()
            llm_span.set_attribute("llm.input_tokens", usage.request_tokens or 0)
            llm_span.set_attribute("llm.output_tokens", usage.response_tokens or 0)

        span.set_attribute("task.status", "completed")
        return {"result": result.data}
```

### Tempo (trace backend) in Compose

```yaml
services:
  tempo:
    image: grafana/tempo:2.4.0
    networks:
      - monitoring
      - backend
    volumes:
      - ./monitoring/tempo-config.yml:/etc/tempo/tempo-config.yml:ro
      - tempo-data:/tmp/tempo
    command: -config.file=/etc/tempo/tempo-config.yml
    profiles:
      - monitoring
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1",
             "--spider", "http://localhost:3200/ready"]
      interval: 10s
      retries: 5

  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.100.0
    networks:
      - monitoring
      - backend
    volumes:
      - ./monitoring/otel-collector-config.yml:/etc/otelcol/config.yml:ro
    command: --config=/etc/otelcol/config.yml
    profiles:
      - monitoring
    ports:
      - "127.0.0.1:4317:4317"    # gRPC receiver (services send traces here)
      - "127.0.0.1:4318:4318"    # HTTP receiver

volumes:
  tempo-data:
```

```yaml
# monitoring/otel-collector-config.yml

receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 1s

exporters:
  otlp:
    endpoint: tempo:4317
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889    # expose metrics collected via OTLP

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp]
```

Add Tempo as a datasource in Grafana, then enable trace-to-log correlation —
clicking a trace in Grafana opens the corresponding Loki log lines for the
same time window and trace ID.

---

## 5. Resource Limits

Without resource limits, a runaway embedding job or a memory-leaking worker
can consume all available RAM and starve the rest of the stack. On an AI
system where GPU memory is a shared, finite resource, this is particularly
dangerous.

### Setting limits

```yaml
services:
  # Embedding service — memory-heavy, CPU-intensive
  embed:
    deploy:
      resources:
        limits:
          cpus: "4.0"         # max 4 CPU cores
          memory: 5G          # hard ceiling — container is killed if exceeded
        reservations:
          cpus: "1.0"         # guaranteed minimum
          memory: 3G          # guaranteed minimum

  # API — should be lean; if it hits 512MB something is wrong
  api:
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 512M
        reservations:
          cpus: "0.25"
          memory: 128M

  # Worker — moderate memory for agent context windows
  worker:
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 1G
        reservations:
          cpus: "0.5"
          memory: 256M

  # Ollama — needs significant RAM for model weights
  # llama3.2 (3B, fp16) ≈ 6GB; llama3.1 (8B, fp16) ≈ 16GB
  ollama:
    deploy:
      resources:
        limits:
          memory: 10G
        reservations:
          memory: 6G

  # Databases — stable RAM requirements
  db:
    deploy:
      resources:
        limits:
          memory: 2G

  pgvector:
    deploy:
      resources:
        limits:
          memory: 1G

  redis:
    deploy:
      resources:
        limits:
          memory: 256M
```

### Observing limit behavior

```bash
# Live resource usage for all containers
docker stats

# See if a container was OOM-killed
docker inspect argos-embed-1 | grep -A5 "OOMKilled"

# Check current resource allocation
docker compose ps --format json | jq '.[].Name'
```

### OOM killer behavior

When a container hits its memory limit, Docker's OOM killer terminates it.
The container's `restart` policy then determines what happens next:

```yaml
services:
  embed:
    restart: unless-stopped   # restarts after OOM — good for transient spikes
    # restart: "no"           # stays dead — good for catching leaks in dev
```

In development, set `restart: "no"` on services you're debugging so OOM kills
are visible rather than silently restarted. In production, use `unless-stopped`
with alerting on unexpected restarts.

---

## 6. `docker compose watch`

`docker compose watch` is a newer Compose feature (v2.22+) that replaces the
bind mount + `--reload` pattern with explicit sync and rebuild rules. It gives
you fine-grained control over which file changes trigger which action.

```yaml
# compose.override.yaml — watch configuration

services:
  api:
    develop:
      watch:
        # Sync Python source changes — no rebuild needed, uvicorn reloads
        - path: ./src
          action: sync
          target: /app/src

        # Sync config files
        - path: ./config
          action: sync
          target: /app/config

        # Rebuild if dependencies change (pyproject.toml or uv.lock)
        - path: ./pyproject.toml
          action: rebuild
        - path: ./uv.lock
          action: rebuild

        # Rebuild if Dockerfile changes
        - path: ./Dockerfile
          action: rebuild

  worker:
    develop:
      watch:
        - path: ./src
          action: sync
          target: /app/src
        - path: ./pyproject.toml
          action: rebuild
        - path: ./uv.lock
          action: rebuild

  mcp-server:
    develop:
      watch:
        - path: ./mcp-server/src
          action: sync
          target: /app/src
        - path: ./mcp-server/pyproject.toml
          action: rebuild
```

### Running watch mode

```bash
# Start the stack and watch for changes
docker compose watch

# Watch specific services only
docker compose watch api worker
```

### `watch` vs bind mounts

| | Bind mount + `--reload` | `docker compose watch` |
|---|---|---|
| Compose version | Any | v2.22+ |
| Change detection | Filesystem events | Compose polling |
| Granularity | Entire directory | Per-path rules |
| Rebuild on dep change | Manual `--build` | Automatic |
| Performance on macOS | ⚠️ can be slow | ✅ better |
| Sync without reload | ❌ | ✅ (`action: sync`) |
| Sync + restart | ❌ | ✅ (`action: sync+restart`) |

The three `action` values:

- **`sync`** — copy the file into the running container. uvicorn picks up
  the change if `--reload` is enabled. No container restart.
- **`sync+restart`** — sync the file then restart the container. Useful for
  config changes that uvicorn doesn't pick up automatically.
- **`rebuild`** — stop the container, rebuild the image, restart. Used for
  dependency or Dockerfile changes.

---

## 7. The Complete Observability Stack in Compose

Adding the full observability layer to the Argos stack from Module 7:

```yaml
# In compose.yaml, add to services: under the monitoring profile

  prometheus:
    image: prom/prometheus:v2.51.0
    networks:
      - backend
      - monitoring
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./monitoring/alerts:/etc/prometheus/alerts:ro
      - prometheus-data:/prometheus
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.retention.time=30d"
      - "--web.enable-lifecycle"        # allows config reload via POST /-/reload
    ports:
      - "127.0.0.1:9090:9090"
    profiles:
      - monitoring
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1",
             "--spider", "http://localhost:9090/-/healthy"]
      interval: 10s
      retries: 5

  grafana:
    image: grafana/grafana:10.4.0
    networks:
      - frontend
      - monitoring
    volumes:
      - grafana-data:/var/lib/grafana
      - ./monitoring/grafana/datasources:/etc/grafana/provisioning/datasources:ro
      - ./monitoring/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_FEATURE_TOGGLES_ENABLE: "traceqlEditor"
    ports:
      - "127.0.0.1:3000:3000"
    profiles:
      - monitoring
    depends_on:
      - prometheus
      - loki
      - tempo

  loki:
    image: grafana/loki:2.9.0
    networks:
      - monitoring
    volumes:
      - ./monitoring/loki-config.yml:/etc/loki/loki-config.yml:ro
      - loki-data:/loki
    command: -config.file=/etc/loki/loki-config.yml
    profiles:
      - monitoring

  promtail:
    image: grafana/promtail:2.9.0
    networks:
      - monitoring
    volumes:
      - ./monitoring/promtail-config.yml:/etc/promtail/config.yml:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    command: -config.file=/etc/promtail/config.yml
    profiles:
      - monitoring
    depends_on:
      - loki

  tempo:
    image: grafana/tempo:2.4.0
    networks:
      - monitoring
      - backend
    volumes:
      - ./monitoring/tempo-config.yml:/etc/tempo/tempo-config.yml:ro
      - tempo-data:/tmp/tempo
    command: -config.file=/etc/tempo/tempo-config.yml
    profiles:
      - monitoring

  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.100.0
    networks:
      - monitoring
      - backend
    volumes:
      - ./monitoring/otel-collector-config.yml:/etc/otelcol/config.yml:ro
    command: --config=/etc/otelcol/config.yml
    ports:
      - "127.0.0.1:4317:4317"
    profiles:
      - monitoring
    depends_on:
      - tempo
      - loki

  mongo-exporter:
    image: percona/mongodb_exporter:0.40
    networks:
      - backend
      - monitoring
    environment:
      MONGODB_URI: mongodb://${MONGO_USER:-admin}@db:27017
    profiles:
      - monitoring

  redis-exporter:
    image: oliver006/redis_exporter:v1.59.0
    networks:
      - backend
      - monitoring
    environment:
      REDIS_ADDR: redis://redis:6379
    profiles:
      - monitoring

  node-exporter:
    image: prom/node-exporter:v1.8.0
    networks:
      - monitoring
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro
    command:
      - "--path.procfs=/host/proc"
      - "--path.sysfs=/host/sys"
      - "--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($$|/)"
    profiles:
      - monitoring

# Add to volumes:
#   prometheus-data:
#   grafana-data:
#   loki-data:
#   tempo-data:
```

### Starting the full observed stack

```bash
# Development stack without observability (default)
docker compose up -d

# Development stack with full observability
COMPOSE_PROFILES=monitoring docker compose up -d

# Open Grafana
open http://localhost:3000   # admin / admin (change this)

# Open Prometheus
open http://localhost:9090

# Reload Prometheus config after editing prometheus.yml (no restart needed)
curl -X POST http://localhost:9090/-/reload
```

---

## Practical Exercise

1. **Add Prometheus metrics** to your FastAPI app. At minimum: a counter for
   agent tasks (with status label) and a histogram for task duration. Verify
   the `/metrics` endpoint returns your custom metrics:
   ```bash
   curl http://localhost:8000/metrics | grep agent_
   ```

2. **Start the monitoring profile** and open Grafana. Add Prometheus as a
   datasource (if not auto-provisioned) and create a single panel showing
   agent task rate over time.

3. **Configure structured JSON logging** in your application. Start the
   monitoring profile and query Loki in Grafana for logs from the `api`
   service:
   ```logql
   {service="api"} | json | level="error"
   ```

4. **Add resource limits** to every service in your stack. Then deliberately
   trigger an OOM: set the `embed` service memory limit to 256M (too low for
   BGE-M3) and watch what happens:
   ```bash
   docker stats
   docker inspect argos-embed-1 | grep OOMKilled
   ```
   Restore the limit to a safe value.

5. **Switch from bind mounts to `docker compose watch`**. Remove the
   `volumes: ./src:/app/src` bind mount from `compose.override.yaml` and
   replace it with a `develop.watch` block. Verify hot reload still works.

**Stretch goal:** Add a Grafana dashboard panel that correlates agent task
failures (from Prometheus) with error log lines (from Loki) on the same time
axis. When a spike in failures appears in the metrics, you should be able to
click into the corresponding log lines without leaving Grafana.

<details>
<summary>Hint — Prometheus not scraping your service</summary>

Services need to be on a shared network with Prometheus. Check that your
`api` service is on the `backend` or `monitoring` network and that Prometheus
is on the same one. Then verify the target is reachable:

```bash
docker compose exec prometheus \
  wget -qO- http://api:8000/metrics | head -20
```

If that works, the issue is in `prometheus.yml`. If it fails, check networks.

</details>

<details>
<summary>Hint — Loki not receiving logs</summary>

Promtail needs access to the Docker socket and the container log directory.
Both must be mounted read-only:

```yaml
volumes:
  - /var/lib/docker/containers:/var/lib/docker/containers:ro
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

Check Promtail's own logs for connection errors:
```bash
docker compose logs promtail
```

</details>

<details>
<summary>Hint — compose watch not detecting changes</summary>

Run `docker compose watch` in a terminal (not detached) — it prints sync
events as they happen, which makes it easy to see if changes are being
detected. Also confirm you're on Compose v2.22+:

```bash
docker compose version
```

</details>

---

## Key Takeaways

- **Metrics, logs, and traces are three different signals** that answer
  different questions. Metrics: is the system healthy? Logs: what happened?
  Traces: where did the time go? You need all three for an agent stack.
- **Separate `/health` and `/ready` endpoints** on model-heavy services.
  Health means "the process is running." Ready means "the model is loaded
  and the service can handle requests."
- **Structured JSON logging** makes Loki queries dramatically more powerful.
  A `trace_id` field in every log line is the bridge between traces and logs.
- **OpenTelemetry auto-instrumentation** handles FastAPI, httpx, and Redis
  with zero manual span creation. Add manual spans only for the agent-specific
  operations that matter: embedding calls, vector searches, LLM inference.
- **Resource limits protect the stack.** Without them, one badly behaved
  service can starve all others. Set limits conservatively and adjust up
  based on observed usage.
- **`docker compose watch`** is the modern alternative to bind mounts — more
  precise, better performance on macOS, and handles dependency changes with
  `action: rebuild` automatically.
- The **entire observability stack lives in git** alongside the application:
  `prometheus.yml`, alert rules, Grafana datasource provisioning, Loki config,
  Promtail config. No manual dashboard setup on a new machine.

---

## Syllabus Complete

You now have a complete, production-shaped Docker Compose education covering:

| Module | Topic | Duration |
|---|---|---|
| 1 | Mental Model & Core Concepts | ½ day |
| 2 | Building Services | ½ day |
| 3 | Networking & Communication | ½ day |
| 4 | Data & Persistence | ½ day |
| 5 | Real-World Python Stack | 1 day |
| 6 | Dev/Prod Parity | ½ day |
| 7 | AI/Agent Stack Patterns | 1 day |
| 8 | Observability & Operations | ½ day |
| **Total** | | **~5 days** |

The stack built across Modules 5–8 is a direct blueprint for Argos and
dev-rag: FastAPI, MongoDB, pgvector, Redis, BGE-M3, Ollama, an MCP server,
a worker pool, and a full observability layer — all wired together correctly,
all observable, all documented as code.

---

## Further Reading

- [Prometheus Python client](https://github.com/prometheus/client_python)
- [prometheus-fastapi-instrumentator](https://github.com/trallnag/prometheus-fastapi-instrumentator)
- [OpenTelemetry Python](https://opentelemetry-python.readthedocs.io/)
- [Grafana Loki](https://grafana.com/docs/loki/latest/)
- [Grafana Tempo](https://grafana.com/docs/tempo/latest/)
- [docker compose watch](https://docs.docker.com/compose/file-watch/)
- [Compose resource limits](https://docs.docker.com/compose/compose-file/05-services/#resources)
