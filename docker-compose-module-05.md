---
module: 5
title: "Real-World Python Stack"
duration: "1 day (~6–8 hours)"
prerequisites: "Modules 1–4 complete"
---

# Module 5: Real-World Python Stack

## Introduction

The previous four modules each covered one dimension of Compose in isolation:
structure, building, networking, persistence. This module drops all of that
into a single production-shaped project.

We're building a FastAPI application backed by MongoDB, Redis, and pgvector.
It has a migration container, a background worker, hot reload in development,
and a clean separation between dev and prod configuration via
`compose.override.yaml`. By the end you'll have a reference stack you can
clone and adapt for any Python project.

This is also the longest module — take it in sections. The first half builds
the core stack; the second half adds the worker, vector database, and the
dev/prod split.

---

## Learning Objectives

By the end of this module you will be able to:

- Build a complete multi-service Python stack from scratch
- Implement hot reload in development using bind mounts and `watchfiles`
- Use `compose.override.yaml` to layer dev config over a prod-ready base
- Wire a migration container into the startup chain
- Add a background worker service that shares the app's code and config
- Integrate pgvector and ChromaDB as services alongside a primary database

---

## 1. Project Structure

Before writing any Compose config, establish a project layout that
Compose, Python packaging, and your editor all agree on.

```
myapp/
├── compose.yaml                  # prod-ready base (no secrets, no ports)
├── compose.override.yaml         # dev additions — auto-loaded by Compose
├── compose.prod.yaml             # prod overrides (explicit, not auto-loaded)
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── .env                          # gitignored
├── .env.example                  # committed
├── .dockerignore
├── secrets/                      # gitignored
│   └── mongo_password.txt
├── init-scripts/
│   └── 01-pgvector.sql
├── src/
│   └── myapp/
│       ├── __init__.py
│       ├── main.py               # FastAPI app
│       ├── worker.py             # background worker entry point
│       ├── config.py             # settings via pydantic-settings
│       ├── db/
│       │   ├── mongo.py
│       │   ├── postgres.py
│       │   └── redis.py
│       └── routers/
│           ├── health.py
│           └── items.py
└── tests/
```

### Settings with `pydantic-settings`

Before building Compose config, define how your app reads its environment.
This single file becomes the contract between your code and Compose:

```python
# src/myapp/config.py

from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from functools import lru_cache


def read_secret(name: str) -> str | None:
    """Read a Docker secret from /run/secrets/, fall back to None."""
    secret_path = Path(f"/run/secrets/{name}")
    if secret_path.exists():
        return secret_path.read_text().strip()
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Application
    app_env: str = "development"
    log_level: str = "info"
    workers: int = 1

    # MongoDB
    mongo_user: str = "admin"
    mongo_pass: str = ""        # overridden by secret at runtime
    mongo_host: str = "db"
    mongo_port: int = 27017
    mongo_db: str = "myapp"

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379

    # Postgres / pgvector
    pg_user: str = "raguser"
    pg_pass: str = ""
    pg_host: str = "pgvector"
    pg_port: int = 5432
    pg_db: str = "ragdb"

    @property
    def mongo_uri(self) -> str:
        password = read_secret("mongo_password") or self.mongo_pass
        return (
            f"mongodb://{self.mongo_user}:{password}"
            f"@{self.mongo_host}:{self.mongo_port}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"

    @property
    def pg_dsn(self) -> str:
        password = read_secret("pg_password") or self.pg_pass
        return (
            f"postgresql+asyncpg://{self.pg_user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

---

## 2. The Dockerfile

A single multi-stage file serving dev, test, and prod:

```dockerfile
# Dockerfile
# syntax=docker/dockerfile:1

# ── base: shared foundation ────────────────────────────────────────────────
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

# Dependencies copied first — cached until pyproject.toml/uv.lock change
COPY pyproject.toml uv.lock ./


# ── development: all deps, source via bind mount ───────────────────────────
FROM base AS development

RUN uv sync --frozen

# Source is NOT copied here — it comes in via bind mount in compose.override.yaml
# This keeps the image lean and rebuild-free during active development

EXPOSE 8000

CMD ["uvicorn", "myapp.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--reload", \
     "--reload-dir", "/app/src"]


# ── test: development deps + test runner ──────────────────────────────────
FROM development AS test

COPY src/ ./src/
COPY tests/ ./tests/

CMD ["python", "-m", "pytest", "-v"]


# ── production: no dev deps, non-root, source baked in ────────────────────
FROM base AS production

RUN uv sync --frozen --no-dev

COPY src/ ./src/

RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "myapp.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4"]
```

---

## 3. The Base `compose.yaml`

The base file describes the *complete* stack with production-safe defaults.
It has no host port bindings for internal services, no bind mounts for source
code, and no dev-only services. Think of it as the prod config that dev then
modifies.

```yaml
# compose.yaml
name: myapp

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

  # ── Application ───────────────────────────────────────────────────────────
  api:
    build:
      context: .
      target: production          # prod stage by default
    networks:
      - frontend
      - backend
    secrets:
      - mongo_password
      - pg_password
    environment:
      APP_ENV: production
      MONGO_USER: ${MONGO_USER:-admin}
      MONGO_HOST: db
      MONGO_DB: ${MONGO_DB:-myapp}
      REDIS_HOST: redis
      PG_USER: ${PG_USER:-raguser}
      PG_HOST: pgvector
      PG_DB: ${PG_DB:-ragdb}
      LOG_LEVEL: ${LOG_LEVEL:-info}
      WORKERS: ${WORKERS:-4}
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
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
      APP_ENV: production
      MONGO_USER: ${MONGO_USER:-admin}
      MONGO_HOST: db
      MONGO_DB: ${MONGO_DB:-myapp}
      REDIS_HOST: redis
      LOG_LEVEL: ${LOG_LEVEL:-info}
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped

  # ── One-shot migration container ──────────────────────────────────────────
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
    restart: "no"               # never restart a migration container

  # ── Databases ─────────────────────────────────────────────────────────────
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
      MONGO_INITDB_DATABASE: ${MONGO_DB:-myapp}
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    networks:
      - backend
    volumes:
      - redis-data:/data
    command: redis-server --appendonly yes   # enable persistence
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3
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
      start_period: 10s
    restart: unless-stopped

volumes:
  mongo-data:
  redis-data:
  pgdata:
```

---

## 4. `compose.override.yaml` — Dev Additions

Compose automatically merges `compose.override.yaml` into `compose.yaml` when
you run `docker compose up`. No flags needed — it just happens.

The override file should contain *only what differs in development*. Anything
not mentioned here inherits from the base.

```yaml
# compose.override.yaml
# Automatically merged during development. Never use in production.

services:

  api:
    build:
      target: development         # override the build stage
    ports:
      - "127.0.0.1:8000:8000"    # expose to host for local browser/curl
    volumes:
      - ./src:/app/src            # bind mount for hot reload
    environment:
      APP_ENV: development
      LOG_LEVEL: debug
      WORKERS: 1
    # healthcheck is inherited — no change needed

  worker:
    build:
      target: development
    volumes:
      - ./src:/app/src            # same source as api — edits affect both

  migrate:
    build:
      target: development         # use dev image (has alembic in dev deps)

  # ── Dev-only: expose databases to the host for inspection ─────────────────
  db:
    ports:
      - "127.0.0.1:27017:27017"  # accessible from mongosh on the host

  redis:
    ports:
      - "127.0.0.1:6379:6379"    # accessible from redis-cli on the host

  pgvector:
    ports:
      - "127.0.0.1:5432:5432"    # accessible from psql on the host

  # ── Dev-only services (not in base compose.yaml at all) ───────────────────
  mongo-express:
    image: mongo-express:latest
    networks:
      - backend
      - frontend
    ports:
      - "127.0.0.1:8081:8081"
    environment:
      ME_CONFIG_MONGODB_ADMINUSERNAME: ${MONGO_USER:-admin}
      ME_CONFIG_MONGODB_ADMINPASSWORD_FILE: /run/secrets/mongo_password
      ME_CONFIG_MONGODB_URL: mongodb://${MONGO_USER:-admin}@db:27017/
      ME_CONFIG_BASICAUTH: "false"
    secrets:
      - mongo_password
    depends_on:
      db:
        condition: service_healthy
    profiles:
      - debug

  pgadmin:
    image: dpage/pgadmin4:latest
    networks:
      - backend
      - frontend
    ports:
      - "127.0.0.1:5050:80"
    environment:
      PGADMIN_DEFAULT_EMAIL: dev@local.dev
      PGADMIN_DEFAULT_PASSWORD: devpassword
    profiles:
      - debug
```

### How merge works

When you run `docker compose up`, Compose deep-merges the two files. The rules:

- **Scalar values** (strings, numbers): override replaces base
- **Lists** (ports, volumes, environment): override *appends* to base
- **Maps** (environment as a map): override keys replace or add to base keys
- **Services in override only**: added wholesale to the merged config
- **Services in base only**: unchanged

```bash
# See exactly what the merged config looks like
docker compose config
```

This command is invaluable for debugging — it shows the final merged YAML
before anything starts.

---

## 5. Hot Reload in Development

The `--reload` flag in the dev `CMD` tells uvicorn to watch for file changes.
Combined with the bind mount in `compose.override.yaml`, edits to your source
files are reflected immediately without rebuilding the image.

### How the chain works

```
You edit src/myapp/routers/items.py on the host
    │
    ▼
Bind mount syncs the change into /app/src/ in the container
    │
    ▼
uvicorn's --reload-dir /app/src detects the change
    │
    ▼
uvicorn reloads the application (< 1 second)
    │
    ▼
Your next request hits the new code
```

### Watching additional directories

If your app reads config or templates from directories outside `src/`:

```yaml
# In compose.override.yaml
services:
  api:
    command: [
      "uvicorn", "myapp.main:app",
      "--host", "0.0.0.0",
      "--port", "8000",
      "--reload",
      "--reload-dir", "/app/src",
      "--reload-dir", "/app/config"   # also watch config/
    ]
    volumes:
      - ./src:/app/src
      - ./config:/app/config
```

### Worker hot reload

Background workers using `watchfiles` directly get the same experience:

```python
# src/myapp/worker.py

import asyncio
from watchfiles import arun_process
from myapp.config import get_settings


async def main():
    settings = get_settings()
    if settings.app_env == "development":
        # Restart the worker process when source files change
        await arun_process(
            "src/",
            target=run_worker,
        )
    else:
        await run_worker()


async def run_worker():
    """Main worker loop."""
    settings = get_settings()
    # ... your worker logic


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 6. The Migration Container Pattern

Migrations need to run after the database is healthy but before the application
starts accepting traffic. The one-shot container pattern enforces this cleanly.

```
pgvector (healthy)
    │
    ▼
migrate (runs alembic upgrade head, exits 0)
    │
    ▼
api (starts accepting requests)
worker (starts consuming queue)
```

If the migration fails (exits non-zero), `api` and `worker` never start.
This prevents the application from running against a schema it doesn't expect.

### Alembic configuration for Compose

```python
# alembic/env.py — relevant section

from myapp.config import get_settings

settings = get_settings()

# alembic reads the DSN from settings, which reads from Docker secrets
config.set_main_option("sqlalchemy.url", settings.pg_dsn)
```

```bash
# Generate a new migration after changing a model
docker compose run --rm migrate alembic revision --autogenerate -m "add_items_table"

# Check current migration state
docker compose run --rm migrate alembic current

# Roll back one migration
docker compose run --rm migrate alembic downgrade -1
```

The `--rm` flag removes the one-shot container immediately after it exits,
keeping `docker compose ps` clean.

---

## 7. Adding ChromaDB

ChromaDB fits naturally alongside pgvector as a profile-controlled alternative
or complement:

```yaml
# In compose.yaml, under services:
  chroma:
    image: chromadb/chroma:0.5.0    # pin the version
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

# Add to volumes:
# chroma-data:
```

```python
# src/myapp/db/chroma.py

import chromadb
from myapp.config import get_settings
from functools import lru_cache


@lru_cache
def get_chroma_client() -> chromadb.AsyncHttpClient:
    settings = get_settings()
    return chromadb.AsyncHttpClient(
        host=settings.chroma_host,
        port=settings.chroma_port,
    )
```

---

## 8. Common Workflows

Day-to-day commands once the stack is configured:

```bash
# ── Starting and stopping ─────────────────────────────────────────────────

# Start everything in dev (override auto-loaded)
docker compose up -d

# Rebuild after pyproject.toml change, then start
docker compose up -d --build api worker

# Stop everything, preserve data
docker compose down

# Nuclear option — stop and wipe all volumes
docker compose down -v


# ── Logs ─────────────────────────────────────────────────────────────────

# Tail all services
docker compose logs -f

# Tail api and worker only
docker compose logs -f api worker

# See migration output
docker compose logs migrate


# ── One-off commands ─────────────────────────────────────────────────────

# Open a Python shell inside the api container
docker compose exec api python

# Run a script
docker compose exec api python scripts/seed_data.py

# Generate a new migration
docker compose run --rm migrate alembic revision --autogenerate -m "my_change"

# Run tests (uses the test stage)
docker compose run --rm \
  -e APP_ENV=test \
  --build \
  api python -m pytest -v


# ── Debugging ────────────────────────────────────────────────────────────

# Shell into api container
docker compose exec api bash

# Start with mongo-express and pgadmin
docker compose --profile debug up -d

# See the fully merged config
docker compose config

# Check service health
docker compose ps


# ── Production simulation ────────────────────────────────────────────────

# Run prod config without override (explicitly ignore override)
docker compose -f compose.yaml up -d --build
```

---

## Putting It Together — Full Startup Sequence

When you run `docker compose up -d` in development, here's the full sequence:

```
1. Compose reads compose.yaml + compose.override.yaml, merges them
2. Compose creates networks: myapp_frontend, myapp_backend
3. Compose creates volumes: mongo-data, redis-data, pgdata, chroma-data

4. Starts in parallel (no dependencies):
   - db       (mongo:7.0.4)
   - redis    (redis:7-alpine)
   - pgvector (pgvector/pgvector:pg16)

5. Each runs its healthcheck every 5–10 seconds

6. Once pgvector is healthy:
   - migrate runs (alembic upgrade head)
   - migrate exits 0 (success)

7. Once db, redis are healthy AND migrate has completed:
   - api starts (dev stage, bind mount active, hot reload on)
   - worker starts (dev stage, bind mount active)

8. api runs its healthcheck
9. If debug profile active: mongo-express and pgadmin start
```

Total time from `docker compose up -d` to ready:
- Cold start (images not pulled): ~2–3 minutes
- Warm start (images cached, volumes populated): ~15–30 seconds

---

## Practical Exercise

Build the complete stack described in this module:

1. **Set up the project structure** — create the directory layout, `pyproject.toml`,
   and the `src/myapp/` package with `main.py`, `config.py`, and a `/health`
   endpoint.

2. **Write the Dockerfile** with `base`, `development`, `test`, and `production`
   stages.

3. **Write `compose.yaml`** with the full service set: `api`, `worker`, `migrate`,
   `db`, `redis`, `pgvector`. Use secrets for all credentials.

4. **Write `compose.override.yaml`** with host port exposure, bind mounts, and
   the `mongo-express` debug service under the `debug` profile.

5. **Verify the startup chain:** run `docker compose up -d` and watch the logs
   to confirm migration runs before the API starts.

6. **Test hot reload:** start the stack, then edit a route in `items.py` and
   confirm the change is live without rebuilding.

7. **Run `docker compose config`** and read through the merged output.
   Identify at least three places where the override changed the base.

**Stretch goal:** Add a `test` service that uses the `test` build stage and
runs `pytest`. Wire it so `docker compose run --rm test` runs the full test
suite against the live `db`, `redis`, and `pgvector` services.

<details>
<summary>Hint — hot reload not working</summary>

Check that the bind mount path matches `--reload-dir`. If the container path
is `/app/src` and your source is at `src/myapp/`, the reload dir should be
`/app/src`, not `/app/src/myapp`. Also confirm the override file is actually
being loaded with `docker compose config`.

</details>

<details>
<summary>Hint — migration container exits before database is ready</summary>

The `migrate` service needs `depends_on.pgvector.condition: service_healthy`.
The `pgvector` service needs a working `healthcheck` block. Check both.
Use `docker compose ps` to see health status and `docker compose logs migrate`
to see the actual error.

</details>

<details>
<summary>Hint — secrets not found</summary>

Create the `secrets/` directory and write the password files:

```bash
mkdir secrets
echo "devmongopw" > secrets/mongo_password.txt
echo "devpgpw"    > secrets/pg_password.txt
```

Add `secrets/` to `.gitignore`. Confirm each service has both `secrets:` at
the top level and `secrets:` under the service definition.

</details>

---

## Key Takeaways

- **`compose.yaml` is the prod-safe base.** No host ports for internal services,
  no bind mounts, production build targets throughout.
- **`compose.override.yaml` is the dev layer.** Auto-loaded — no flags needed.
  Contains only what differs: build targets, ports, bind mounts, dev-only services.
- **`docker compose config`** shows the merged result. Always run this when
  something behaves unexpectedly.
- Hot reload requires three things working together: the `--reload` flag, the
  `--reload-dir` pointing to the mounted path, and a bind mount in the override.
- **Migration containers** must use `restart: "no"` and be depended on with
  `condition: service_completed_successfully`. A failed migration should block
  the application from starting.
- `docker compose run --rm` is for one-off commands — migrations, scripts, test
  runs — that shouldn't leave containers around after they exit.
- Workers share the application's code, config, and secrets. They get the same
  bind mount in dev, so edits affect both API and worker simultaneously.

---

## Further Reading

- [Compose merge rules](https://docs.docker.com/compose/multiple-compose-files/merge/)
- [uvicorn `--reload` docs](https://www.uvicorn.org/settings/#development)
- [watchfiles documentation](https://watchfiles.helpmanual.io/)
- [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)

---

## Next Module

Module 6 covers dev/prod parity in depth: `compose.prod.yaml` overrides, CI
pipeline patterns, building and tagging images for a registry, and using
`profiles:` to toggle the monitoring stack.
