# Docker Compose for Python Developers

A practical, intermediate-level course on Docker Compose for Python developers
who already know the basics and want to build production-shaped stacks — not
toy examples.

By the end of this course you will have built a complete AI agent infrastructure:
FastAPI, MongoDB, pgvector, Redis, BGE-M3, Ollama, an MCP server, a worker pool,
and a full observability layer — all wired together correctly, all observable,
all documented as code.

---

## Who This Is For

You are comfortable with:
- Python and FastAPI
- Basic Docker (`pull`, `run`, `build`, `exec`)
- Running a multi-service app locally with shell scripts or manually

You want to:
- Replace those shell scripts with a single `compose.yaml`
- Understand networking, volumes, and secrets well enough to design them
  deliberately rather than cargo-culting examples
- Build a local AI/agent stack that mirrors what you'd run in production
- Have a reference you can adapt for any Python project

---

## What You Build

The course follows a single project arc. Each module adds a layer:

```
Module 1–2   compose.yaml skeleton + optimized Dockerfile
Module 3     Network topology with real security boundaries
Module 4     Durable storage, vector databases, secrets management
Module 5     Full Python stack: API + worker + migration container
Module 6     Dev/prod parity: override files, CI pipeline, image pushing
Module 7     AI layer: Ollama, BGE-M3, MCP server, agent worker pool
Module 8     Observability: Prometheus, Grafana, Loki, OpenTelemetry, resource limits
```

The final stack (Modules 5–8) is a direct blueprint for an Argos-style agent
orchestration system backed by a dev-rag storage layer. You can run it as-is
or adapt it to any Python AI project.

---

## Prerequisites

- Docker Desktop or Docker Engine (v24+) with Compose v2.22+
- Python 3.12 and `uv` installed locally
- Familiarity with FastAPI and Pydantic
- Basic understanding of what a container is (we skip the fundamentals)

```bash
# Verify your setup
docker --version          # Docker version 24+
docker compose version    # Docker Compose version v2.22+
uv --version
```

---

## Modules

### [Module 1 — Mental Model & Core Concepts](./docker-compose-module-01.md)
**Duration:** ½ day

The foundation. What Compose adds over raw `docker run`, how `compose.yaml`
is structured, every core CLI command with its important flags, and how to
manage environment variables and `.env` files correctly. Closes with profiles
— the mechanism that lets you toggle optional services cleanly.

**Key concepts:** service lifecycle, service-name DNS, `.env` files,
`docker compose up/down/logs/exec/ps`, profiles

---

### [Module 2 — Building Services](./docker-compose-module-02.md)
**Duration:** ½ day

How to write Dockerfiles that work well inside a Compose workflow. Layer cache
ordering for fast rebuilds, multi-stage builds that produce separate dev and
prod images from a single file, and the `depends_on` / `healthcheck` system
that prevents startup race conditions.

**Key concepts:** build context, `.dockerignore`, layer caching, multi-stage
builds, `build:` vs `image:`, `depends_on` conditions, `service_completed_successfully`

---

### [Module 3 — Networking & Communication](./docker-compose-module-03.md)
**Duration:** ½ day

How Compose's embedded DNS makes service-name resolution work, and how to use
custom networks to enforce real security boundaries. A service that shares no
network with another service cannot reach it — no firewall rules required.

**Key concepts:** bridge network, DNS resolver, custom networks, multi-network
services, `ports:` vs `expose:`, loopback binding, external networks for
cross-project communication

---

### [Module 4 — Data & Persistence](./docker-compose-module-04.md)
**Duration:** ½ day

Named volumes for databases, bind mounts for source code — and why mixing them
up causes either data loss or performance problems. Volume strategies for
MongoDB, pgvector, ChromaDB, and Qdrant. Secrets management across three levels
of sensitivity: `.env` files, environment injection, and Docker secrets.

**Key concepts:** named volumes, bind mounts, `tmpfs`, macOS performance,
init scripts, shared volumes, Docker secrets, `/run/secrets/`

---

### [Module 5 — Real-World Python Stack](./docker-compose-module-05.md)
**Duration:** 1 day

Everything from Modules 1–4 assembled into one production-shaped project.
The base `compose.yaml` is the prod description; `compose.override.yaml` adds
dev affordances on top. Hot reload via uvicorn and bind mounts. Migration
container wired into the startup chain. Background worker sharing the app's
code and config.

**Key concepts:** `compose.override.yaml` auto-merge, hot reload, migration
containers, `restart: "no"`, `docker compose config`, `docker compose run --rm`,
`pydantic-settings` for config, worker services

---

### [Module 6 — Dev/Prod Parity](./docker-compose-module-06.md)
**Duration:** ½ day

The multi-file Compose strategy: base + override + prod + CI. `tmpfs` volumes
for ephemeral CI databases. Image tagging and pushing to a registry. A complete
GitHub Actions workflow that tests on push and builds/pushes on tags. The most
common dev/prod parity gaps and how to close them before they become bugs.

**Key concepts:** `compose.prod.yaml`, `compose.ci.yaml`, `tmpfs`, `--exit-code-from`,
`--pull always`, multi-platform builds, GitHub Actions, parity gaps

---

### [Module 7 — AI/Agent Stack Patterns](./docker-compose-module-07.md)
**Duration:** 1 day

The AI-specific layer. Running Ollama with model pre-loading and GPU passthrough.
BGE-M3 as a dedicated embedding service with a proper `/ready` healthcheck.
Profile-controlled vector backend switching between pgvector, ChromaDB, and
Qdrant. An MCP server as a first-class Compose service. An agent worker pool
that scales horizontally via Redis Streams.

**Key concepts:** cold-start problem, model volume sharing, GPU passthrough,
`/health` vs `/ready`, vector backend abstraction, MCP-as-service, worker
scaling, on-demand ingest pipeline

---

### [Module 8 — Observability & Operations](./docker-compose-module-08.md)
**Duration:** ½ day

The full observability stack as Compose services under the `monitoring` profile:
Prometheus for metrics (including custom agent counters and histograms), Grafana
with provisioned dashboards, Loki and Promtail for centralized structured logging,
and OpenTelemetry + Tempo for distributed tracing across the agent pipeline.
Closes with resource limits and `docker compose watch`.

**Key concepts:** Prometheus, Grafana-as-code, LogQL, structured JSON logging,
OpenTelemetry, trace-to-log correlation, resource limits, OOM behavior,
`docker compose watch`

---

## Total Duration

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

Work through modules sequentially. Each exercise builds on the last —
by Module 7 you have a running stack to instrument in Module 8.

---

## The Stack

These are the technologies used across the course. None are required before
you start; each is introduced in the module that uses it.

**Application**
- [FastAPI](https://fastapi.tiangolo.com/) — async Python web framework
- [Pydantic AI](https://ai.pydantic.dev/) — agent framework
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — config management
- [uv](https://docs.astral.sh/uv/) — Python package and project manager
- [uvicorn](https://www.uvicorn.org/) — ASGI server with hot reload
- [watchfiles](https://watchfiles.helpmanual.io/) — worker hot reload

**Databases and Storage**
- [MongoDB 7](https://www.mongodb.com/) — document store
- [pgvector](https://github.com/pgvector/pgvector) — PostgreSQL with vector search
- [ChromaDB](https://www.trychroma.com/) — vector database (migration baseline)
- [Qdrant](https://qdrant.tech/) — vector database (evaluation target)
- [Redis 7](https://redis.io/) — task queue and cache

**AI and Inference**
- [Ollama](https://ollama.com/) — local LLM inference
- [BGE-M3](https://github.com/FlagOpen/FlagEmbedding) — embedding model (dense + sparse + ColBERT)
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework

**Observability**
- [Prometheus](https://prometheus.io/) — metrics collection and alerting
- [Grafana](https://grafana.com/) — dashboards and visualization
- [Loki](https://grafana.com/oss/loki/) — log aggregation
- [Promtail](https://grafana.com/docs/loki/latest/send-data/promtail/) — log collector
- [Tempo](https://grafana.com/oss/tempo/) — distributed tracing backend
- [OpenTelemetry](https://opentelemetry.io/) — instrumentation standard

---

## Quick Reference

### Commands you'll use every day

```bash
# Start everything (override auto-loaded in dev)
docker compose up -d

# Rebuild after pyproject.toml or Dockerfile changes
docker compose up -d --build api

# See what's running and healthy
docker compose ps

# Tail logs for specific services
docker compose logs -f api worker

# Shell into a running service
docker compose exec api bash

# Run a one-off command (migration, test, script)
docker compose run --rm api python scripts/seed.py

# See the merged config (essential debugging tool)
docker compose config

# Stop everything, preserve volumes
docker compose down

# Stop and wipe all data
docker compose down -v
```

### Multi-file patterns

```bash
# Development (override auto-loaded)
docker compose up -d

# Production simulation locally
docker compose -f compose.yaml -f compose.prod.yaml up -d

# CI test run
docker compose -f compose.yaml -f compose.ci.yaml run --rm test

# With monitoring profile
COMPOSE_PROFILES=monitoring docker compose up -d

# With GPU support (Module 7)
COMPOSE_PROFILES=gpu docker compose up -d

# Full stack: monitoring + Qdrant evaluation
COMPOSE_PROFILES=monitoring,qdrant docker compose up -d
```

### Key file layout

```
project/
├── compose.yaml               # base — prod-safe defaults
├── compose.override.yaml      # dev — auto-loaded, bind mounts, debug ports
├── compose.prod.yaml          # prod — registry images, resource limits
├── compose.ci.yaml            # CI — tmpfs volumes, test runner
├── Dockerfile                 # multi-stage: base / development / test / production
├── .env                       # gitignored — real credentials
├── .env.example               # committed — placeholder values, documented
├── .dockerignore
├── secrets/                   # gitignored — Docker secret files
├── init-scripts/              # SQL run once on database first start
├── monitoring/
│   ├── prometheus.yml
│   ├── alerts/
│   ├── loki-config.yml
│   ├── promtail-config.yml
│   ├── tempo-config.yml
│   ├── otel-collector-config.yml
│   └── grafana/
│       ├── datasources/
│       └── dashboards/
└── src/
```

---

## Design Decisions

A few deliberate choices made throughout the course, with reasoning:

**`uv` over `pip`**
`uv` is used exclusively for all Python package management. It's faster,
produces reproducible lockfiles, and integrates cleanly with multi-stage
Dockerfiles via `uv sync --frozen --no-dev`.

**Base file is prod, not dev**
`compose.yaml` describes the production stack. `compose.override.yaml` adds
dev affordances on top. This means the base file is always production-safe —
you never accidentally deploy a dev configuration.

**Named volumes for databases, always**
Even in development. On macOS and Windows, bind-mounting database storage
crosses a VM boundary and causes serious write performance degradation.
Named volumes live inside the VM and avoid this entirely.

**`depends_on` always uses `condition:`**
`depends_on: - db` (without a condition) waits for the container to start,
not for the service to be ready. This causes startup race conditions that look
like random failures. Always pair `depends_on` with `condition: service_healthy`
and a real `healthcheck`.

**`/health` and `/ready` are different endpoints**
`/health`: the process is running. `/ready`: the model is loaded and the
service can handle requests. Using `/health` as a healthcheck on model-heavy
services causes dependent services to start before the model is available.

**Secrets in files, not environment variables**
Environment variables are visible via `docker inspect` to anyone with Docker
socket access. Docker secrets mounted at `/run/secrets/` are not. Use the
file-based approach for anything genuinely sensitive.

---

## Book Chapter Candidates

If you're using this material as a source for a book on AI agent programming,
these sections translate most directly to standalone chapters:

| Topic | Module | Why it works as a chapter |
|---|---|---|
| Layer caching (wrong vs right order) | 2 | Clear before/after, memorable |
| `ports:` vs `expose:`, network topology | 3 | Connectivity matrix is a strong figure |
| Secrets level table | 4 | Decision guide readers bookmark |
| `compose.override.yaml` merge model | 5 | Removes a common misconception |
| Parity gaps (5 archetypes) | 6 | Each gap = a concrete bug pattern |
| Cold-start and `/health` vs `/ready` | 7 | Genuinely novel for most readers |
| Metrics/logs/traces framing | 8 | Strong chapter opener |

---

## Further Reading

**Official documentation**
- [Docker Compose file reference](https://docs.docker.com/compose/compose-file/)
- [Compose networking](https://docs.docker.com/compose/networking/)
- [Multi-file Compose](https://docs.docker.com/compose/multiple-compose-files/)
- [docker compose watch](https://docs.docker.com/compose/file-watch/)

**Python tooling**
- [uv Docker integration](https://docs.astral.sh/uv/guides/integration/docker/)
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [OpenTelemetry Python](https://opentelemetry-python.readthedocs.io/)
- [prometheus-fastapi-instrumentator](https://github.com/trallnag/prometheus-fastapi-instrumentator)

**AI stack**
- [Ollama Docker](https://hub.docker.com/r/ollama/ollama)
- [BGE-M3 / FlagEmbedding](https://github.com/FlagOpen/FlagEmbedding)
- [Qdrant quickstart](https://qdrant.tech/documentation/quickstart/)
- [Pydantic AI MCP](https://ai.pydantic.dev/mcp/)

**Background reading**
- [Twelve-Factor App](https://12factor.net/) — the philosophy behind dev/prod parity
- [Dockerfile best practices](https://docs.docker.com/develop/develop-images/dockerfile_best-practices/)
