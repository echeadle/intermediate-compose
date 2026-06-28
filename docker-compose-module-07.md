---
module: 7
title: "AI/Agent Stack Patterns"
duration: "1 day (~6–8 hours)"
prerequisites: "Modules 1–6 complete; familiarity with FastAPI, Pydantic AI, and basic LLM concepts"
---

# Module 7: AI/Agent Stack Patterns

## Introduction

The previous six modules covered Compose patterns that apply to any Python
stack. This module applies all of them to the specific problem of running AI
agent systems locally — and doing it in a way that's close enough to production
to trust.

Agent stacks have a different shape from typical web applications. They're
compute-heavy rather than I/O-heavy. They have long-running inference processes
that aren't request-scoped. They mix CPU-bound embedding workloads with
GPU-accelerated generation. They need vector databases alongside document
stores. And they often have asynchronous task pipelines that look nothing like
a traditional API.

Compose handles all of this — but only if you structure it deliberately.

This module builds a reference Compose stack for an Argos-style agent
orchestration system backed by dev-rag's storage layer: FastAPI, MongoDB,
pgvector, Qdrant, ChromaDB, an MCP server, and Ollama for local inference.
Everything you need to develop and test a full agent system without touching
a cloud API.

---

## Learning Objectives

By the end of this module you will be able to:

- Run Ollama as a Compose service with GPU passthrough for local LLM inference
- Wire BGE-M3 embedding and reranker services into a Compose stack
- Compose a full RAG storage layer (pgvector, ChromaDB, Qdrant) with profile-
  controlled switching between backends
- Structure an MCP server as a first-class Compose service
- Design an agent worker pool with controlled concurrency
- Manage the unique cold-start problem of model-heavy services

---

## 1. The Cold-Start Problem

AI services have a startup characteristic that databases don't: **model loading
time**. A FastAPI service starts in under a second. An Ollama instance pulling
a 7B model takes minutes. A BGE-M3 embedding service loading from disk takes
10–30 seconds.

This changes how you write healthchecks and `depends_on` chains.

### Aggressive healthcheck timing for model services

Standard healthcheck settings assume a service is ready in seconds. Model
services need a longer `start_period` — the grace period before failures count:

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 120s    # two minutes before failures count
```

### Model pre-loading on startup

Ollama loads models lazily by default — the first request triggers the load.
In a Compose stack that's unacceptable because your first agent request will
time out waiting for the model.

Fix it with an init service that pulls and loads the model before the agent
starts:

```yaml
services:
  ollama-init:
    image: ollama/ollama:latest
    command: >
      sh -c "
        ollama serve &
        sleep 5 &&
        ollama pull ${OLLAMA_MODEL:-llama3.2} &&
        ollama pull ${OLLAMA_EMBED_MODEL:-nomic-embed-text} &&
        wait
      "
    volumes:
      - ollama-models:/root/.ollama
    restart: "no"           # one-shot, exits after pull completes

  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama    # same volume — models already there
    depends_on:
      ollama-init:
        condition: service_completed_successfully
```

After `ollama-init` exits, the models are cached in the shared volume.
When `ollama` starts, they're available immediately — no re-download.

---

## 2. Running Ollama with GPU Passthrough

Ollama without GPU access falls back to CPU inference — functional but slow
for anything larger than a 3B model. Compose supports GPU passthrough on
Linux hosts with the NVIDIA Container Toolkit installed.

### GPU configuration

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama
    networks:
      - backend
    ports:
      - "127.0.0.1:11434:11434"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1               # reserve 1 GPU
              capabilities: [gpu]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 120s
    restart: unless-stopped
    profiles:
      - gpu                           # only active when GPU is available
```

### CPU fallback for machines without GPU

Use profiles to provide both a GPU and a CPU variant:

```yaml
services:
  # GPU variant — needs NVIDIA Container Toolkit
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama
    networks:
      - backend
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    profiles:
      - gpu
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 120s

  # CPU variant — works everywhere, slower on large models
  ollama-cpu:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama    # shares the same model cache
    networks:
      - backend
    profiles:
      - cpu
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 180s               # CPU loads slower
```

```bash
# On a machine with NVIDIA GPU
COMPOSE_PROFILES=gpu docker compose up -d

# On a CPU-only machine (development laptop, CI)
COMPOSE_PROFILES=cpu docker compose up -d
```

Your application code talks to `ollama:11434` in both cases. When only the
cpu profile is active, the `ollama-cpu` container registers on the backend
network under the alias `ollama-cpu` — so you need a config toggle, or
use an nginx alias service. Simpler: just keep both services named `ollama`
and use mutually exclusive profiles:

```yaml
# Cleaner: separate compose files per profile
# compose.gpu.yaml and compose.cpu.yaml, both defining a service named "ollama"
```

---

## 3. BGE-M3 Embedding Service

BGE-M3 is heavy enough that you don't want it loading inside your main API
process. It's better as a dedicated service that the API calls over HTTP,
keeping the API container lean and independently restartable.

### Embedding service container

```dockerfile
# embed-service/Dockerfile

FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Cache HuggingFace models in a known location
    HF_HOME=/app/model-cache \
    TRANSFORMERS_CACHE=/app/model-cache

COPY embed-service/pyproject.toml embed-service/uv.lock ./
RUN uv sync --frozen --no-dev

COPY embed-service/src/ ./src/

EXPOSE 8001

CMD ["uvicorn", "embed_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

```python
# embed-service/src/embed_service/main.py

from fastapi import FastAPI
from pydantic import BaseModel
from FlagEmbedding import BGEM3FlagModel
from functools import lru_cache
import os

app = FastAPI(title="BGE-M3 Embedding Service")


@lru_cache(maxsize=1)
def get_model() -> BGEM3FlagModel:
    """Load model once, cache forever. Triggered on first request."""
    model_name = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    return BGEM3FlagModel(model_name, use_fp16=True)


class EmbedRequest(BaseModel):
    texts: list[str]
    batch_size: int = 12
    return_dense: bool = True
    return_sparse: bool = False
    return_colbert: bool = False


class EmbedResponse(BaseModel):
    dense: list[list[float]] | None = None
    sparse: list[dict] | None = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """Separate readiness check — only healthy after model loads."""
    try:
        model = get_model()
        return {"status": "ready", "model": model.model.config.name_or_path}
    except Exception as e:
        return {"status": "loading", "error": str(e)}, 503


@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest):
    model = get_model()
    output = model.encode(
        request.texts,
        batch_size=request.batch_size,
        return_dense=request.return_dense,
        return_sparse=request.return_sparse,
        return_colbert_vecs=request.return_colbert,
    )
    return EmbedResponse(
        dense=output.get("dense_vecs", None),
        sparse=output.get("lexical_weights", None),
    )
```

### Wiring it into Compose

```yaml
services:
  embed:
    build:
      context: .
      dockerfile: embed-service/Dockerfile
    networks:
      - backend
    volumes:
      - hf-model-cache:/app/model-cache    # persist downloaded models
    environment:
      EMBED_MODEL: ${EMBED_MODEL:-BAAI/bge-m3}
    deploy:
      resources:
        limits:
          memory: 4G                        # BGE-M3 needs ~3GB at fp16
    healthcheck:
      # Use /ready — waits for model to actually load, not just uvicorn to start
      test: ["CMD", "curl", "-f", "http://localhost:8001/ready"]
      interval: 20s
      timeout: 15s
      retries: 10
      start_period: 90s                     # model download on first run
    restart: unless-stopped

volumes:
  hf-model-cache:                           # survives restarts — no re-download
```

### Calling the embedding service from the API

```python
# src/myapp/db/embeddings.py

import httpx
from myapp.config import get_settings
from functools import lru_cache


@lru_cache(maxsize=1)
def get_embed_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=f"http://{settings.embed_host}:{settings.embed_port}",
        timeout=30.0,       # embedding can be slow for large batches
    )


async def embed_texts(texts: list[str]) -> list[list[float]]:
    client = get_embed_client()
    response = await client.post(
        "/embed",
        json={"texts": texts, "return_dense": True},
    )
    response.raise_for_status()
    return response.json()["dense"]
```

---

## 4. Vector Database Strategy

The dev-rag stack supports ChromaDB today and is migrating toward pgvector,
with Qdrant as a future option for larger-scale deployments. Compose profiles
make it clean to run any combination without running all three simultaneously.

```yaml
# compose.yaml — vector database services

services:

  # ── pgvector: primary migration target ────────────────────────────────────
  pgvector:
    image: pgvector/pgvector:pg16
    networks:
      - backend
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init-scripts/01-extensions.sql:/docker-entrypoint-initdb.d/01-extensions.sql:ro
    secrets:
      - pg_password
    environment:
      POSTGRES_USER: ${PG_USER:-raguser}
      POSTGRES_DB: ${PG_DB:-ragdb}
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "${PG_USER:-raguser}"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

  # ── ChromaDB: current backend during migration ────────────────────────────
  chroma:
    image: chromadb/chroma:0.5.0
    networks:
      - backend
    volumes:
      - chroma-data:/chroma/chroma
    environment:
      ANONYMIZED_TELEMETRY: "false"
      ALLOW_RESET: "false"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/heartbeat"]
      interval: 10s
      timeout: 5s
      retries: 3
    restart: unless-stopped
    profiles:
      - chroma                      # explicit opt-in

  # ── Qdrant: future evaluation ─────────────────────────────────────────────
  qdrant:
    image: qdrant/qdrant:v1.9.0    # pin the version
    networks:
      - backend
    volumes:
      - qdrant-data:/qdrant/storage
      - ./qdrant-config.yaml:/qdrant/config/production.yaml:ro
    ports:
      - "127.0.0.1:6333:6333"      # REST (dev inspection only)
      - "127.0.0.1:6334:6334"      # gRPC
    environment:
      QDRANT__SERVICE__GRPC_PORT: "6334"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/readyz"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s
    restart: unless-stopped
    profiles:
      - qdrant                      # explicit opt-in

volumes:
  pgdata:
  chroma-data:
  qdrant-data:
```

### Qdrant configuration file

```yaml
# qdrant-config.yaml

storage:
  storage_path: /qdrant/storage

service:
  host: 0.0.0.0
  http_port: 6333
  grpc_port: 6334

# Optimize for BGE-M3 dense vectors (1024 dimensions)
# Collections configured at runtime via API
```

### Vector backend abstraction in Python

Rather than hardcoding which vector store to use, build a thin abstraction
your application talks to. Compose controls which backend is running; Python
controls which client to instantiate:

```python
# src/myapp/db/vector_store.py

from enum import Enum
from myapp.config import get_settings


class VectorBackend(str, Enum):
    PGVECTOR = "pgvector"
    CHROMA = "chroma"
    QDRANT = "qdrant"


def get_vector_store():
    """Return the configured vector store client."""
    settings = get_settings()
    backend = VectorBackend(settings.vector_backend)

    if backend == VectorBackend.PGVECTOR:
        from myapp.db.pgvector_store import PgVectorStore
        return PgVectorStore(dsn=settings.pg_dsn)

    if backend == VectorBackend.CHROMA:
        import chromadb
        return chromadb.AsyncHttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
        )

    if backend == VectorBackend.QDRANT:
        from qdrant_client import AsyncQdrantClient
        return AsyncQdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            grpc_port=settings.qdrant_grpc_port,
            prefer_grpc=True,
        )

    raise ValueError(f"Unknown vector backend: {backend}")
```

```bash
# Run with pgvector (default, no profile needed)
docker compose up -d

# Evaluate Qdrant alongside pgvector
VECTOR_BACKEND=qdrant docker compose --profile qdrant up -d

# Run full ChromaDB migration testing
VECTOR_BACKEND=chroma docker compose --profile chroma up -d
```

---

## 5. MCP Server as a Compose Service

An MCP server is a long-running process that exposes tools to AI agents. In a
Compose stack it's a first-class service — not a subprocess launched by the
agent, but an independently deployable unit with its own network identity.

### MCP server service

```yaml
services:
  mcp-server:
    build:
      context: .
      dockerfile: mcp-server/Dockerfile
      target: development
    networks:
      - backend
    volumes:
      - ./mcp-server/src:/app/src       # hot reload in dev
    environment:
      MONGO_HOST: db
      MONGO_DB: ${MONGO_DB:-ragdb}
      PG_HOST: pgvector
      PG_DB: ${PG_DB:-ragdb}
      EMBED_HOST: embed
      EMBED_PORT: "8001"
      VECTOR_BACKEND: ${VECTOR_BACKEND:-pgvector}
      LOG_LEVEL: ${LOG_LEVEL:-info}
    secrets:
      - mongo_password
      - pg_password
    depends_on:
      db:
        condition: service_healthy
      pgvector:
        condition: service_healthy
      embed:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/health"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 45s
    restart: unless-stopped
```

```python
# mcp-server/src/mcp_server/main.py

from mcp.server.fastmcp import FastMCP
from myapp.db.vector_store import get_vector_store
from myapp.db.embeddings import embed_texts
from myapp.config import get_settings

mcp = FastMCP("dev-rag")
settings = get_settings()


@mcp.tool()
async def search_corpus(
    query: str,
    domain: str = "all",
    top_k: int = 10,
) -> list[dict]:
    """
    Hybrid search across the RAG corpus.
    Combines dense vector search with BM25 keyword scoring.
    """
    store = get_vector_store()
    query_embedding = await embed_texts([query])
    results = await store.hybrid_search(
        query=query,
        embedding=query_embedding[0],
        domain=domain,
        top_k=top_k,
    )
    return results


@mcp.tool()
async def get_chunk(chunk_id: str) -> dict:
    """Retrieve a specific chunk by ID with full metadata."""
    store = get_vector_store()
    return await store.get_chunk(chunk_id)


@mcp.tool()
async def list_domains() -> list[str]:
    """List all available corpus domains."""
    store = get_vector_store()
    return await store.list_domains()


if __name__ == "__main__":
    import uvicorn
    # Expose MCP over HTTP for Compose networking
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=8002)
```

### Connecting agents to the MCP server

```python
# src/myapp/agents/research_agent.py

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerHTTP
from myapp.config import get_settings


def get_research_agent() -> Agent:
    settings = get_settings()

    # Agent talks to MCP server by service name — Compose DNS handles routing
    mcp_server = MCPServerHTTP(
        url=f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"
    )

    return Agent(
        model=f"ollama/{settings.ollama_model}",  # or anthropic/claude-... in prod
        mcp_servers=[mcp_server],
        system_prompt=(
            "You are a research assistant with access to a curated technical corpus. "
            "Use search_corpus to find relevant information before answering."
        ),
    )
```

---

## 6. Agent Worker Pool

An agent orchestration system like Argos needs workers that pull tasks from a
queue and run agent pipelines. In Compose, this is a scaled worker service.

### Worker service with task queue

```yaml
services:
  # ── Task queue (Redis Streams or Redis Queue) ──────────────────────────────
  redis:
    image: redis:7-alpine
    networks:
      - backend
    volumes:
      - redis-data:/data
    command: redis-server --appendonly yes
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 3

  # ── Agent API: accepts tasks, enqueues them ───────────────────────────────
  api:
    build:
      context: .
      target: development
    networks:
      - frontend
      - backend
    environment:
      REDIS_HOST: redis
      MCP_HOST: mcp-server
      MCP_PORT: "8002"
      OLLAMA_HOST: ollama
      OLLAMA_PORT: "11434"
      OLLAMA_MODEL: ${OLLAMA_MODEL:-llama3.2}
    depends_on:
      redis:
        condition: service_healthy
      mcp-server:
        condition: service_healthy

  # ── Agent worker: dequeues and executes tasks ─────────────────────────────
  worker:
    build:
      context: .
      target: development
    command: ["python", "-m", "myapp.worker"]
    networks:
      - backend
    volumes:
      - ./src:/app/src
    environment:
      REDIS_HOST: redis
      MCP_HOST: mcp-server
      MCP_PORT: "8002"
      OLLAMA_HOST: ollama
      OLLAMA_PORT: "11434"
      OLLAMA_MODEL: ${OLLAMA_MODEL:-llama3.2}
      WORKER_CONCURRENCY: ${WORKER_CONCURRENCY:-2}
    depends_on:
      redis:
        condition: service_healthy
      mcp-server:
        condition: service_healthy
      ollama:
        condition: service_healthy
    restart: unless-stopped
    deploy:
      replicas: ${WORKER_REPLICAS:-1}    # scale via env var
```

### Scaling workers

```bash
# Run two worker instances
WORKER_REPLICAS=2 docker compose up -d worker

# Or scale after startup
docker compose up -d --scale worker=3
```

Each worker instance is identical — same image, same config, competing for
tasks on the Redis queue. Redis handles distribution; you don't need any
inter-worker coordination.

### Worker implementation

```python
# src/myapp/worker.py

import asyncio
import redis.asyncio as redis
from myapp.config import get_settings
from myapp.agents.research_agent import get_research_agent
import json
import logging

logger = logging.getLogger(__name__)


async def process_task(task: dict) -> dict:
    """Run an agent task and return the result."""
    agent = get_research_agent()
    result = await agent.run(task["prompt"])
    return {
        "task_id": task["task_id"],
        "result": result.data,
        "usage": result.usage().model_dump(),
    }


async def worker_loop(concurrency: int = 2):
    settings = get_settings()
    r = redis.Redis(host=settings.redis_host, port=settings.redis_port)

    semaphore = asyncio.Semaphore(concurrency)   # limit concurrent agent runs
    logger.info(f"Worker started with concurrency={concurrency}")

    async def handle_task(raw_task: bytes):
        async with semaphore:
            task = json.loads(raw_task)
            logger.info(f"Processing task {task['task_id']}")
            try:
                result = await process_task(task)
                await r.xadd("results", {"data": json.dumps(result)})
            except Exception as e:
                logger.error(f"Task {task['task_id']} failed: {e}")
                await r.xadd("failed", {"task_id": task["task_id"], "error": str(e)})

    while True:
        # Redis Streams: blocking read with 5s timeout
        messages = await r.xread({"tasks": "$"}, block=5000, count=concurrency)
        if messages:
            _, entries = messages[0]
            tasks = [asyncio.create_task(handle_task(data[b"data"]))
                     for _, data in entries]
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    settings = get_settings()
    asyncio.run(worker_loop(concurrency=settings.worker_concurrency))
```

---

## 7. The Full Argos-Style Stack

Putting it all together: a complete Compose stack for an agent orchestration
system with RAG, local inference, and an MCP tool layer.

```yaml
# compose.yaml
name: argos

secrets:
  mongo_password:
    file: ./secrets/mongo_password.txt
  pg_password:
    file: ./secrets/pg_password.txt

networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge

services:

  # ── Inference ──────────────────────────────────────────────────────────────
  ollama-init:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama
    entrypoint: >
      sh -c "
        ollama serve &
        sleep 5 &&
        ollama pull ${OLLAMA_MODEL:-llama3.2} &&
        ollama pull ${OLLAMA_EMBED_MODEL:-nomic-embed-text} &&
        echo 'Models ready' && exit 0
      "
    restart: "no"

  ollama:
    image: ollama/ollama:latest
    networks:
      - backend
    volumes:
      - ollama-models:/root/.ollama
    depends_on:
      ollama-init:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 60s
    restart: unless-stopped

  embed:
    build:
      context: .
      dockerfile: embed-service/Dockerfile
    networks:
      - backend
    volumes:
      - hf-model-cache:/app/model-cache
    environment:
      EMBED_MODEL: ${EMBED_MODEL:-BAAI/bge-m3}
    deploy:
      resources:
        limits:
          memory: 4G
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/ready"]
      interval: 20s
      timeout: 15s
      retries: 10
      start_period: 90s
    restart: unless-stopped

  # ── Storage ────────────────────────────────────────────────────────────────
  db:
    image: mongo:7.0.4
    networks:
      - backend
    volumes:
      - mongo-data:/data/db
    secrets:
      - mongo_password
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${MONGO_USER:-admin}
      MONGO_INITDB_ROOT_PASSWORD_FILE: /run/secrets/mongo_password
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s
    restart: unless-stopped

  pgvector:
    image: pgvector/pgvector:pg16
    networks:
      - backend
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init-scripts:/docker-entrypoint-initdb.d:ro
    secrets:
      - pg_password
    environment:
      POSTGRES_USER: ${PG_USER:-raguser}
      POSTGRES_DB: ${PG_DB:-ragdb}
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "${PG_USER:-raguser}"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    networks:
      - backend
    volumes:
      - redis-data:/data
    command: redis-server --appendonly yes
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3
    restart: unless-stopped

  qdrant:
    image: qdrant/qdrant:v1.9.0
    networks:
      - backend
    volumes:
      - qdrant-data:/qdrant/storage
    profiles:
      - qdrant
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/readyz"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s
    restart: unless-stopped

  # ── Migration ──────────────────────────────────────────────────────────────
  migrate:
    build:
      context: .
      target: production
    command: ["python", "-m", "alembic", "upgrade", "head"]
    networks:
      - backend
    secrets:
      - pg_password
    environment:
      PG_USER: ${PG_USER:-raguser}
      PG_HOST: pgvector
      PG_DB: ${PG_DB:-ragdb}
    depends_on:
      pgvector:
        condition: service_healthy
    restart: "no"

  # ── MCP Tool Layer ─────────────────────────────────────────────────────────
  mcp-server:
    build:
      context: .
      dockerfile: mcp-server/Dockerfile
    networks:
      - backend
    environment:
      MONGO_HOST: db
      PG_HOST: pgvector
      EMBED_HOST: embed
      EMBED_PORT: "8001"
      VECTOR_BACKEND: ${VECTOR_BACKEND:-pgvector}
    secrets:
      - mongo_password
      - pg_password
    depends_on:
      db:
        condition: service_healthy
      pgvector:
        condition: service_healthy
      embed:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/health"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 45s
    restart: unless-stopped

  # ── Agent API and Workers ──────────────────────────────────────────────────
  api:
    build:
      context: .
      target: production
    networks:
      - frontend
      - backend
    secrets:
      - mongo_password
      - pg_password
    environment:
      MONGO_HOST: db
      PG_HOST: pgvector
      REDIS_HOST: redis
      MCP_HOST: mcp-server
      MCP_PORT: "8002"
      OLLAMA_HOST: ollama
      OLLAMA_MODEL: ${OLLAMA_MODEL:-llama3.2}
      VECTOR_BACKEND: ${VECTOR_BACKEND:-pgvector}
    depends_on:
      mcp-server:
        condition: service_healthy
      ollama:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped

  worker:
    build:
      context: .
      target: production
    command: ["python", "-m", "myapp.worker"]
    networks:
      - backend
    secrets:
      - mongo_password
      - pg_password
    environment:
      REDIS_HOST: redis
      MCP_HOST: mcp-server
      MCP_PORT: "8002"
      OLLAMA_HOST: ollama
      OLLAMA_MODEL: ${OLLAMA_MODEL:-llama3.2}
      WORKER_CONCURRENCY: ${WORKER_CONCURRENCY:-2}
    depends_on:
      mcp-server:
        condition: service_healthy
      ollama:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped
    deploy:
      replicas: ${WORKER_REPLICAS:-1}

volumes:
  mongo-data:
  pgdata:
  redis-data:
  qdrant-data:
  ollama-models:
  hf-model-cache:
```

### Full startup sequence

```
Phase 1 — parallel:
  ollama-init  (pulls llama3.2 and nomic-embed-text into shared volume)
  db           (MongoDB — waiting for healthcheck)
  pgvector     (waiting for healthcheck)
  redis        (waiting for healthcheck)

Phase 2 — after ollama-init completes:
  ollama       (Ollama server — models already in volume, fast start)

Phase 2 — after pgvector healthy:
  migrate      (alembic upgrade head — exits 0)

Phase 3 — after db, pgvector, embed all healthy:
  embed        (BGE-M3 — slow start, ~90s on first run)

Phase 4 — after db, pgvector, embed healthy AND migrate completed:
  mcp-server   (RAG tool layer)

Phase 5 — after mcp-server, ollama, redis all healthy:
  api          (FastAPI — accepts traffic)
  worker       (agent task runner)
```

---

## 8. Ingest Pipeline Service

The dev-rag ingest pipeline is a natural Compose service — it runs on demand
(or on a schedule) and writes to the storage layer that the MCP server reads.

```yaml
services:
  ingest:
    build:
      context: .
      target: production
    command: ["python", "-m", "myapp.ingest.pipeline", "--source", "/corpus"]
    networks:
      - backend
    volumes:
      - corpus-input:/corpus:ro         # documents mount here
      - ./ingest-config.yaml:/app/ingest-config.yaml:ro
    secrets:
      - mongo_password
      - pg_password
    environment:
      MONGO_HOST: db
      PG_HOST: pgvector
      EMBED_HOST: embed
      EMBED_PORT: "8001"
      VECTOR_BACKEND: ${VECTOR_BACKEND:-pgvector}
      NOVELTY_THRESHOLD: ${NOVELTY_THRESHOLD:-0.85}
    depends_on:
      embed:
        condition: service_healthy
      pgvector:
        condition: service_healthy
      db:
        condition: service_healthy
    restart: "no"                       # runs once per invocation
    profiles:
      - ingest

volumes:
  corpus-input:
```

```bash
# Run ingest against the live stack
docker compose --profile ingest run --rm ingest

# Ingest a specific directory from the host
docker run --rm \
  --network argos_backend \
  --volume /path/to/my/docs:/corpus:ro \
  argos-ingest:latest \
  python -m myapp.ingest.pipeline --source /corpus
```

---

## Practical Exercise

Build the agent stack described in this module in stages:

1. **Start with inference:** get Ollama running with the init pattern. Confirm
   a model is pre-loaded by running:
   ```bash
   docker compose exec ollama ollama list
   ```

2. **Add the embedding service:** build and start the BGE-M3 container. Watch
   the `/ready` endpoint until the model is loaded. Time the first embedding
   request versus the second (cache should make the second nearly instant).

3. **Add the storage layer:** bring up MongoDB, pgvector, and Redis with
   healthchecks. Run the migration container and confirm it exits 0.

4. **Build the MCP server:** implement at least one tool (`search_corpus` or a
   stub). Verify it's reachable from the API container:
   ```bash
   docker compose exec api curl http://mcp-server:8002/health
   ```

5. **Write a simple agent:** using Pydantic AI, create an agent that calls the
   MCP server and runs a query through Ollama. Submit a task via the API and
   confirm a worker picks it up from Redis.

6. **Test scaling:** run `docker compose up -d --scale worker=3` and submit
   multiple tasks simultaneously. Confirm they're processed in parallel.

**Stretch goal:** implement the novelty filter from dev-rag (`novelty_filter.py`)
as a step in the ingest pipeline. Wire it to the embedding service over HTTP so
the filter uses the same BGE-M3 instance as the rest of the stack.

<details>
<summary>Hint — Ollama model pre-loading not working</summary>

The `ollama serve` command starts the server but doesn't block. The `sleep 5`
gives it time to initialize before `ollama pull` runs. If the pull still fails,
increase the sleep. Check the init container's logs:
```bash
docker compose logs ollama-init
```

</details>

<details>
<summary>Hint — BGE-M3 healthcheck failing</summary>

The `/health` endpoint returns 200 as soon as uvicorn starts. The `/ready`
endpoint only returns 200 after the model finishes loading. Use `/ready` for
the healthcheck, not `/health`, or dependent services will start before the
model is actually available.

</details>

<details>
<summary>Hint — MCP server can't reach the embedding service</summary>

Both services need to be on the same network (`backend`). Check with:
```bash
docker compose exec mcp-server curl http://embed:8001/ready
```
If that fails, run `docker compose config` and verify both services list
`backend` under `networks`.

</details>

---

## Key Takeaways

- AI services have a **cold-start problem** that web services don't. Use longer
  `start_period` values and separate `/health` (is uvicorn up?) from `/ready`
  (is the model loaded?) endpoints.
- **Model volume sharing** between an init container and the main service
  eliminates re-downloads across restarts. The init container runs once; the
  main service inherits the populated volume.
- **Run embedding as a dedicated service**, not inside the API process. It keeps
  the API container lean, independently restartable, and scalable separately.
- **GPU passthrough** uses `deploy.resources.reservations.devices` and requires
  the NVIDIA Container Toolkit on the host. Use profiles to provide CPU and GPU
  variants.
- **Profiles control which vector backend is active.** The Python abstraction
  layer makes the application code identical regardless of which backend Compose
  is running.
- The **MCP server is a first-class Compose service** — not a subprocess. It has
  its own network identity, healthcheck, and dependency chain.
- **Agent workers scale horizontally** with `--scale` or `deploy.replicas`. Redis
  Streams handles distribution — no inter-worker coordination needed.
- A `restart: "no"` ingest service under a profile gives you an on-demand pipeline
  that shares the live stack's storage without being part of the always-on stack.

---

## Further Reading

- [Ollama Docker documentation](https://hub.docker.com/r/ollama/ollama)
- [NVIDIA Container Toolkit install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
- [FlagEmbedding BGE-M3](https://github.com/FlagOpen/FlagEmbedding/tree/master/FlagEmbedding/BGE_M3)
- [Qdrant Docker quickstart](https://qdrant.tech/documentation/quickstart/)
- [Pydantic AI MCP integration](https://ai.pydantic.dev/mcp/)
- [Redis Streams](https://redis.io/docs/manual/data-types/streams/)
- [FastMCP](https://github.com/jlowin/fastmcp)

---

## Next Module

Module 8 closes the syllabus with observability and operations: adding
Prometheus metrics to the agent API, tracing agent runs with OpenTelemetry,
centralized logging with Loki, and setting resource limits that prevent a
runaway embedding job from starving the API.
