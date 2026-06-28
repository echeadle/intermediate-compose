---
module: 2
title: "Building Services"
duration: "½ day (~3–4 hours)"
prerequisites: "Module 1 complete; comfortable reading a basic Dockerfile"
---

# Module 2: Building Services

## Introduction

Module 1 gave you a working `compose.yaml`. But one line in that file carries
a lot of hidden complexity:

```yaml
services:
  api:
    build: .    # ← this line
```

That single dot says "build an image from the Dockerfile in the current
directory." What happens next — how Docker caches layers, how the build context
is constructed, how the image differs between dev and prod — determines whether
your builds are fast and reproducible or slow and fragile.

This module covers how to write Dockerfiles that work *well* inside a Compose
workflow: layer caching that actually helps, multi-stage builds that produce
lean production images from the same file, and the `depends_on` / `healthcheck`
system that prevents startup race conditions.

---

## Learning Objectives

By the end of this module you will be able to:

- Write a Python `Dockerfile` with correct layer ordering for fast cache hits
- Use multi-stage builds to produce separate dev and prod images from one file
- Use `build:` and `image:` correctly depending on the context
- Configure `depends_on` and `healthcheck` to enforce safe startup ordering
- Explain what the build context is and why a `.dockerignore` file matters

---

## 1. Build Context and Why It Matters

When Docker processes `build: .`, it sends everything in the current directory
to the Docker daemon as the **build context**. This happens before a single
line of your `Dockerfile` is executed.

If your project directory contains a Python virtualenv, a `.git` folder, test
fixtures, or cached embeddings, Docker ships all of it — even if none of it
ends up in the image. Large build contexts slow down every build.

The fix is a `.dockerignore` file, which works exactly like `.gitignore`:

```
# .dockerignore

# Python
__pycache__/
*.py[cod]
*.pyo
.venv/
venv/
.uv/
uv.lock         # include this in the image only if you want reproducible builds

# Development artifacts
.pytest_cache/
.mypy_cache/
.ruff_cache/
htmlcov/
.coverage

# Data and models (can be large)
data/
models/
*.gguf
chroma_db/

# Version control
.git/
.gitignore

# Editor config
.vscode/
.idea/
*.swp

# Compose files (not needed inside the container)
compose.yaml
compose.override.yaml
.env
.env.*
```

> **Rule:** If a file doesn't need to be inside the container, put it in
> `.dockerignore`. Your build context should be small and tight.

---

## 2. Layer Caching — The Most Important Concept

Docker builds images as a stack of **layers**, one per `RUN`, `COPY`, or `ADD`
instruction. It caches each layer and reuses it on subsequent builds — unless
something upstream changed, which invalidates that layer and every layer below
it.

This means **order is everything**. Things that change rarely go at the top.
Things that change often go at the bottom.

### The wrong order (slow every build)

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# ❌ Copies ALL source code first — any .py change invalidates dependency install
COPY . .

RUN pip install -r requirements.txt

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Every time you change a single line of Python, Docker reinstalls every
dependency. On a real project this adds minutes to every build cycle.

### The right order (fast rebuilds)

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# ✅ Copy ONLY the dependency spec first
COPY pyproject.toml uv.lock ./

# Install dependencies — this layer is cached until pyproject.toml changes
RUN pip install uv && uv sync --frozen --no-dev

# NOW copy source code — cache miss here only reinstalls nothing
COPY src/ ./src/

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Cache invalidation chain:
- `pyproject.toml` unchanged → dependency layer is cached → only `COPY src/` runs → fast
- `pyproject.toml` changed → dependency layer rebuilds → expected, and correct

### Layer caching with `uv`

Since you use `uv` exclusively, here's the idiomatic pattern:

```dockerfile
FROM python:3.12-slim

# Install uv itself (pinned for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first — cached until pyproject.toml or uv.lock changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Source code last
COPY src/ ./src/

# Use the venv uv created
ENV PATH="/app/.venv/bin:$PATH"

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 3. Multi-Stage Builds

A multi-stage build uses multiple `FROM` instructions in a single Dockerfile.
Each stage can copy artifacts from a previous stage, and only the final stage
ships in the image. This lets you use heavy build tools (compilers, test runners,
linters) without bloating the production image.

### Single-file dev and prod images

```dockerfile
# syntax=docker/dockerfile:1

# ── Stage 1: base ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./


# ── Stage 2: development ───────────────────────────────────────────────────
FROM base AS development

# Install ALL dependencies including dev tools (pytest, ruff, mypy, etc.)
RUN uv sync --frozen

COPY . .

# Hot reload via watchfiles
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--reload"]


# ── Stage 3: production ────────────────────────────────────────────────────
FROM base AS production

# Install only runtime dependencies
RUN uv sync --frozen --no-dev

COPY src/ ./src/

# Run as non-root for security
RUN adduser --disabled-password --gecos "" appuser
USER appuser

# No --reload in production
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4"]
```

### Targeting a stage from Compose

```yaml
# compose.yaml (development)
services:
  api:
    build:
      context: .
      target: development    # ← selects the "development" stage
    volumes:
      - ./src:/app/src       # bind mount for hot reload
    ports:
      - "8000:8000"
```

```yaml
# compose.prod.yaml (production override)
services:
  api:
    build:
      context: .
      target: production    # ← selects the "production" stage
    # no bind mount in prod
```

Run production locally:
```bash
docker compose -f compose.yaml -f compose.prod.yaml up -d --build
```

### What you gain

| | Dev image | Prod image |
|---|---|---|
| Has pytest, ruff, mypy | ✅ | ❌ |
| Source via bind mount | ✅ (editable) | ❌ |
| Hot reload enabled | ✅ | ❌ |
| Runs as root | ✅ (convenient) | ❌ (non-root) |
| Image size | Larger | Smaller |

---

## 4. `build:` vs `image:` — Choosing the Right One

Every service in `compose.yaml` needs either a `build:` or an `image:` key.

```yaml
services:
  # build: — Docker builds the image from source
  api:
    build:
      context: .          # where to find the Dockerfile
      dockerfile: Dockerfile   # optional if named "Dockerfile"
      target: development      # optional stage target
      args:                    # optional build-time variables
        APP_ENV: development

  # image: — Docker pulls a pre-built image from a registry
  db:
    image: mongo:7

  # both: — build locally but tag it for pushing
  worker:
    build: .
    image: myorg/myapp-worker:latest   # tag applied to the built image
```

### Decision guide

Use `build:` when:
- It's your own application code
- You need different dev/prod configurations
- You're iterating and need `--build` to reflect changes

Use `image:` when:
- It's a third-party service (database, cache, queue)
- You want a specific, pinned version for reproducibility
- The image comes from a private registry you've already authenticated to

> **Pin your third-party images.** `mongo:latest` is a trap — it can change
> under you. Use `mongo:7.0.4` or at minimum `mongo:7` (major version pinned).

### Build arguments

Build args are available only during the build — not at runtime. Use them for
configuration that affects *how the image is built*, not how the container runs:

```dockerfile
ARG APP_ENV=production

RUN if [ "$APP_ENV" = "development" ]; then \
      uv sync --frozen; \
    else \
      uv sync --frozen --no-dev; \
    fi
```

```yaml
services:
  api:
    build:
      context: .
      args:
        APP_ENV: development
```

For runtime configuration, use `environment:` instead (covered in Module 1).

---

## 5. `depends_on` and `healthchecks` — Startup Ordering

By default, Compose starts services in parallel. If your API tries to connect
to MongoDB before MongoDB is ready to accept connections, it crashes on startup.
`depends_on` fixes this — but only if you use it correctly.

### `depends_on` without a healthcheck (not good enough)

```yaml
# ❌ This only waits for the container to START, not for MongoDB to be READY
services:
  api:
    depends_on:
      - db

  db:
    image: mongo:7
```

MongoDB's container starts in milliseconds, but the mongod process needs several
seconds to initialize. Your API will still crash.

### `depends_on` with `condition: service_healthy` (correct)

```yaml
services:
  api:
    build: .
    depends_on:
      db:
        condition: service_healthy    # wait for db healthcheck to pass
      redis:
        condition: service_healthy

  db:
    image: mongo:7
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 10s       # check every 10 seconds
      timeout: 5s         # fail if check takes longer than 5s
      retries: 5          # mark unhealthy after 5 consecutive failures
      start_period: 20s   # grace period before failures count

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3
```

### Healthchecks for your own services

Your application containers should also expose healthchecks. FastAPI makes this
easy with a `/health` endpoint:

```python
# src/main.py
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok"}
```

```yaml
services:
  api:
    build: .
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s   # give uvicorn time to start
```

Now downstream services (workers, a reverse proxy) can wait for your API to be
genuinely ready before connecting.

### The three `condition` values

```yaml
depends_on:
  db:
    condition: service_started    # container is running (default — not useful)
  db:
    condition: service_healthy    # healthcheck passes ✅ use this
  db:
    condition: service_completed_successfully   # for one-shot init containers
```

`service_completed_successfully` is useful for database migration containers
that run once and exit:

```yaml
services:
  api:
    depends_on:
      migrate:
        condition: service_completed_successfully

  migrate:
    build: .
    command: ["python", "-m", "alembic", "upgrade", "head"]
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgres:16
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "postgres"]
      interval: 5s
      retries: 5
```

The startup chain becomes: `db` → (healthy) → `migrate` → (completed) → `api`.

---

## Putting It Together — Full Example

A FastAPI service with correct caching, multi-stage build, and safe startup:

```dockerfile
# Dockerfile

# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./

FROM base AS development
RUN uv sync --frozen
COPY . .
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS production
RUN uv sync --frozen --no-dev
COPY src/ ./src/
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

```yaml
# compose.yaml

services:
  api:
    build:
      context: .
      target: development
    ports:
      - "${API_PORT:-8000}:8000"
    environment:
      MONGO_URI: mongodb://${MONGO_USER}:${MONGO_PASS}@db:27017
    volumes:
      - ./src:/app/src        # hot reload in dev
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s

  db:
    image: mongo:7.0.4        # pinned version
    volumes:
      - mongo-data:/data/db
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${MONGO_USER}
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_PASS}
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s

volumes:
  mongo-data:
```

---

## Practical Exercise

Take the `compose.yaml` you built in Module 1's exercise and upgrade it:

1. **Convert the Dockerfile** to a multi-stage build with `development` and
   `production` stages. Dev should include all dev dependencies and enable
   hot reload. Prod should run as a non-root user.

2. **Add a `.dockerignore`** that excludes your virtualenv, caches, `.env`
   files, and any local data directories.

3. **Add healthchecks** to both your FastAPI service and MongoDB. Make the API
   service depend on MongoDB with `condition: service_healthy`.

4. **Add a `/health` endpoint** to your FastAPI app that returns `{"status": "ok"}`.

5. Verify the startup order is correct by watching `docker compose up` logs
   and confirming the API only starts after MongoDB is healthy.

**Stretch goal:** Add a one-shot `migrate` service that prints "Running
migrations..." and exits with code 0. Wire it between MongoDB and the API using
`condition: service_completed_successfully`.

<details>
<summary>Hint — multi-stage build</summary>

Use `FROM python:3.12-slim AS base`, then `FROM base AS development`, then
`FROM base AS production`. The `base` stage installs `uv` and copies
`pyproject.toml`. The dev and prod stages each call `uv sync` with different
flags.

</details>

<details>
<summary>Hint — startup chain</summary>

```
db (healthy) → api (started)
```

Set `depends_on.db.condition: service_healthy` in the `api` service. Make sure
the `db` service has a working `healthcheck` block.

</details>

---

## Key Takeaways

- **Build context is sent to Docker before building.** Use `.dockerignore`
  aggressively to keep it small.
- **Layer order determines cache efficiency.** Dependencies before source code —
  always.
- **Multi-stage builds** let one Dockerfile serve dev and prod. Use `target:` in
  Compose to select the right stage.
- **`image:` for third-party services, `build:` for your own code.** Pin image
  versions.
- **`depends_on` without a healthcheck is nearly useless.** Always pair it with
  `condition: service_healthy` and a real healthcheck command.
- A `/health` endpoint on your own services enables downstream dependencies to
  wait safely.

---

## Further Reading

- [Dockerfile best practices (Docker docs)](https://docs.docker.com/develop/develop-images/dockerfile_best-practices/)
- [Multi-stage builds](https://docs.docker.com/build/building/multi-stage/)
- [uv Docker integration guide](https://docs.astral.sh/uv/guides/integration/docker/)
- [Compose `depends_on` reference](https://docs.docker.com/compose/compose-file/05-services/#depends_on)

---

## Next Module

Module 3 goes deeper on Compose networking: how the automatic bridge network
works, service-name DNS resolution under the hood, custom networks for
multi-service isolation, and how to control which services are reachable from
the host.
