---
module: 6
title: "Dev/Prod Parity"
duration: "½ day (~3–4 hours)"
prerequisites: "Module 5 complete; basic familiarity with GitHub Actions or any CI system"
---

# Module 6: Dev/Prod Parity

## Introduction

Module 5 introduced the `compose.override.yaml` pattern. This module goes
deeper on the full philosophy behind it: keeping your development environment
as close to production as possible so that "works on my machine" becomes a
smaller and smaller category of problems.

Dev/prod parity is one of the twelve factors from the Twelve-Factor App
methodology. The core idea is that the gap between dev and prod — in
dependencies, services, and configuration — is where bugs live. Compose gives
you the tools to close that gap almost entirely.

This module covers the complete multi-file Compose strategy, image tagging and
pushing for deployment, profiles as an environment toggle, and how to wire
Compose into a GitHub Actions CI pipeline.

---

## Learning Objectives

By the end of this module you will be able to:

- Manage dev, CI, and prod environments with a disciplined multi-file strategy
- Build, tag, and push production images from a Compose workflow
- Use `profiles:` to layer monitoring and tooling onto any environment
- Write a GitHub Actions workflow that tests, builds, and pushes using Compose
- Identify and close the most common dev/prod parity gaps

---

## 1. The Multi-File Strategy

Compose supports merging an arbitrary number of files with the `-f` flag. A
clean multi-environment strategy uses three files:

```
compose.yaml              # base: complete service definitions, prod defaults
compose.override.yaml     # dev: auto-loaded, adds dev ports/mounts/tools
compose.prod.yaml         # prod: explicit overrides for production deployment
```

And optionally:

```
compose.ci.yaml           # CI: test-focused, no persistence, fast teardown
```

### The base file is not "dev config with some things removed"

This is the most common mistake. The base file should be the *production*
description of your stack, written as if it will run in prod. The override
file adds what dev needs on top. Not the other way around.

```
base        = complete, prod-safe, no dev affordances
+ override  = base + bind mounts + debug ports + dev tools   (development)
+ prod      = base + registry images + resource limits         (production)
+ ci        = base + ephemeral volumes + test commands         (CI)
```

### How Compose merges multiple files

```bash
# Development (override auto-loaded)
docker compose up -d

# Equivalent explicit form
docker compose -f compose.yaml -f compose.override.yaml up -d

# Production
docker compose -f compose.yaml -f compose.prod.yaml up -d

# CI
docker compose -f compose.yaml -f compose.ci.yaml up -d

# See the merged result before running
docker compose -f compose.yaml -f compose.prod.yaml config
```

---

## 2. `compose.prod.yaml` — Production Overrides

The prod file tightens what the base defined: it pins image tags from a
registry, sets resource limits, disables dev affordances, and configures
production-grade restart behavior.

```yaml
# compose.prod.yaml

services:

  api:
    # Pull a pre-built, tagged image instead of building locally
    image: ghcr.io/yourorg/myapp-api:${IMAGE_TAG:-latest}
    build: null                   # disable build in prod — image comes from registry
    environment:
      APP_ENV: production
      LOG_LEVEL: ${LOG_LEVEL:-warning}
      WORKERS: ${WORKERS:-4}
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 1G
        reservations:
          cpus: "0.5"
          memory: 256M
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"

  worker:
    image: ghcr.io/yourorg/myapp-worker:${IMAGE_TAG:-latest}
    build: null
    deploy:
      resources:
        limits:
          cpus: "1.0"
          memory: 512M
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"

  migrate:
    image: ghcr.io/yourorg/myapp-api:${IMAGE_TAG:-latest}
    build: null

  # Databases: tighten resource limits, no port exposure
  db:
    # ports: is absent — not exposed to host in prod
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

### Deploying to a server

```bash
# On the prod server, after pulling the latest compose files:
IMAGE_TAG=v1.4.2 \
  docker compose -f compose.yaml -f compose.prod.yaml \
  up -d --pull always
```

`--pull always` forces Docker to check the registry for a newer version of
each image, even if one with that tag is cached locally.

---

## 3. `compose.ci.yaml` — CI-Specific Configuration

CI environments have different priorities from both dev and prod:

- **Speed over durability** — ephemeral volumes, no persistence between runs
- **Isolation** — each run starts clean
- **Determinism** — same input, same output, every time
- **No interactive tools** — no browser UIs, no admin panels

```yaml
# compose.ci.yaml

services:

  api:
    build:
      context: .
      target: test              # use the test build stage
    environment:
      APP_ENV: test
      LOG_LEVEL: debug
    # No ports needed — tests talk to services on the internal network

  worker:
    build:
      context: .
      target: test

  migrate:
    build:
      context: .
      target: test

  # Databases in CI: minimal config, no persistence needed
  db:
    # Override the named volume with a tmpfs for speed
    volumes:
      - type: tmpfs
        target: /data/db        # lives in memory — fast, auto-clears on stop

  redis:
    volumes:
      - type: tmpfs
        target: /data

  pgvector:
    volumes:
      - type: tmpfs
        target: /var/lib/postgresql/data

  # One-shot test runner service
  test:
    build:
      context: .
      target: test
    command: ["python", "-m", "pytest", "-v", "--tb=short"]
    networks:
      - backend
    environment:
      APP_ENV: test
      MONGO_HOST: db
      REDIS_HOST: redis
      PG_HOST: pgvector
      # In CI, credentials come from environment, not secret files
      MONGO_PASS: ${MONGO_PASS:-cipassword}
      PG_PASS: ${PG_PASS:-cipassword}
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
```

### `tmpfs` for CI databases

`tmpfs` mounts live entirely in memory. They're faster than disk volumes and
automatically disappear when the container stops — exactly what CI needs.
There's no leftover state from a previous run, no volume cleanup step required.

```yaml
volumes:
  - type: tmpfs
    target: /data/db
    tmpfs:
      size: 536870912    # 512MB limit (optional)
```

---

## 4. Profiles as Environment Toggles

Profiles aren't just for optional dev tools (as shown in Module 1). They're
also a clean way to toggle entire subsystems — a monitoring stack, a tracing
layer, a load test harness — across any environment.

### Monitoring profile

```yaml
# In compose.yaml, alongside your main services:

  prometheus:
    image: prom/prometheus:v2.51.0
    profiles:
      - monitoring
    networks:
      - backend
      - monitoring
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    ports:
      - "127.0.0.1:9090:9090"

  grafana:
    image: grafana/grafana:10.4.0
    profiles:
      - monitoring
    networks:
      - frontend
      - monitoring
    volumes:
      - grafana-data:/var/lib/grafana
      - ./monitoring/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
      - ./monitoring/grafana/datasources:/etc/grafana/provisioning/datasources:ro
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}
    depends_on:
      - prometheus

  # Metrics exporter sidecar for MongoDB
  mongo-exporter:
    image: percona/mongodb_exporter:0.40
    profiles:
      - monitoring
    networks:
      - backend
      - monitoring
    environment:
      MONGODB_URI: mongodb://${MONGO_USER:-admin}@db:27017
    command: --collect-all

  node-exporter:
    image: prom/node-exporter:v1.8.0
    profiles:
      - monitoring
    networks:
      - monitoring
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro
    command:
      - "--path.procfs=/host/proc"
      - "--path.sysfs=/host/sys"

volumes:
  prometheus-data:
  grafana-data:
```

### Prometheus configuration

```yaml
# monitoring/prometheus.yml

global:
  scrape_interval: 15s

scrape_configs:
  - job_name: "api"
    static_configs:
      - targets: ["api:8000"]    # service name resolves on the backend network
    metrics_path: "/metrics"

  - job_name: "mongodb"
    static_configs:
      - targets: ["mongo-exporter:9216"]

  - job_name: "node"
    static_configs:
      - targets: ["node-exporter:9100"]
```

### FastAPI metrics endpoint

```python
# src/myapp/main.py — add Prometheus metrics

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI()

# Exposes /metrics automatically
Instrumentator().instrument(app).expose(app)
```

### Using the monitoring profile

```bash
# Development without monitoring (default)
docker compose up -d

# Development with monitoring stack
docker compose --profile monitoring up -d

# Prod with monitoring
docker compose \
  -f compose.yaml \
  -f compose.prod.yaml \
  --profile monitoring \
  up -d

# COMPOSE_PROFILES env var (useful in .env)
COMPOSE_PROFILES=monitoring docker compose up -d
```

---

## 5. Image Tagging and Pushing

Compose can build and push images to a registry as part of a deployment
workflow. This is the bridge between local development and production.

### Image naming in `compose.yaml`

```yaml
services:
  api:
    build:
      context: .
      target: production
    image: ghcr.io/yourorg/myapp-api:${IMAGE_TAG:-dev}
```

When both `build:` and `image:` are present, Compose builds the image and
tags it with the `image:` name. Subsequent `push` uses that tag.

### Build, tag, and push workflow

```bash
# Set the tag from git
export IMAGE_TAG=$(git rev-parse --short HEAD)   # e.g. "a3f9c12"

# Or use a semver tag
export IMAGE_TAG=v1.4.2

# Build production images
docker compose \
  -f compose.yaml \
  -f compose.prod.yaml \
  build

# Push to registry
docker compose \
  -f compose.yaml \
  -f compose.prod.yaml \
  push

# Deploy on the server
ssh deploy@prod-server \
  "IMAGE_TAG=${IMAGE_TAG} docker compose \
    -f compose.yaml \
    -f compose.prod.yaml \
    up -d --pull always"
```

### Multi-platform builds (Apple Silicon → Linux server)

If you develop on a Mac with Apple Silicon (M-series) but deploy to a Linux
x86 server, you need to build for the right platform:

```bash
# Build for Linux AMD64 regardless of host architecture
docker buildx build \
  --platform linux/amd64 \
  --target production \
  --tag ghcr.io/yourorg/myapp-api:${IMAGE_TAG} \
  --push \
  .
```

---

## 6. GitHub Actions CI Pipeline

A complete workflow that tests on every push and builds/pushes on tags:

```yaml
# .github/workflows/ci.yml

name: CI

on:
  push:
    branches: [main, develop]
    tags: ["v*"]
  pull_request:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:

  # ── Job 1: Run tests ───────────────────────────────────────────────────────
  test:
    name: Test
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Create CI secrets
        run: |
          mkdir -p secrets
          echo "cipassword" > secrets/mongo_password.txt
          echo "cipassword" > secrets/pg_password.txt

      - name: Create CI env file
        run: |
          cat > .env << EOF
          MONGO_USER=admin
          MONGO_PASS=cipassword
          PG_USER=raguser
          PG_PASS=cipassword
          PG_DB=ragdb_test
          EOF

      - name: Build test images
        run: |
          docker compose \
            -f compose.yaml \
            -f compose.ci.yaml \
            build

      - name: Start infrastructure
        run: |
          docker compose \
            -f compose.yaml \
            -f compose.ci.yaml \
            up -d db redis pgvector

      - name: Wait for services
        run: |
          docker compose \
            -f compose.yaml \
            -f compose.ci.yaml \
            up migrate --exit-code-from migrate

      - name: Run tests
        run: |
          docker compose \
            -f compose.yaml \
            -f compose.ci.yaml \
            run --rm test

      - name: Capture logs on failure
        if: failure()
        run: |
          docker compose \
            -f compose.yaml \
            -f compose.ci.yaml \
            logs

      - name: Tear down
        if: always()
        run: |
          docker compose \
            -f compose.yaml \
            -f compose.ci.yaml \
            down -v


  # ── Job 2: Build and push image ────────────────────────────────────────────
  build-push:
    name: Build & Push
    runs-on: ubuntu-latest
    needs: test                               # only runs if tests pass
    if: startsWith(github.ref, 'refs/tags/')  # only on version tags

    permissions:
      contents: read
      packages: write

    steps:
      - uses: actions/checkout@v4

      - name: Log in to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata for Docker
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository_owner }}/myapp-api
          tags: |
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=sha,prefix=sha-

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          target: production
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha              # GitHub Actions cache
          cache-to: type=gha,mode=max
```

### The `--exit-code-from` flag

```bash
docker compose up migrate --exit-code-from migrate
```

This runs only the `migrate` service (and its dependencies), waits for it to
exit, and returns its exit code to the shell. If migration fails, the CI step
fails. Clean integration with CI pass/fail logic without any custom scripting.

---

## 7. Common Parity Gaps and How to Close Them

These are the most frequent sources of "works in dev, fails in prod":

### Gap 1: Different Python versions

Dev uses Python 3.12.3, prod uses 3.11.8 from an older base image.

```dockerfile
# Close it: pin the exact base image digest
FROM python:3.12.3-slim@sha256:abc123...  AS base
```

Or at minimum pin the minor version:

```dockerfile
FROM python:3.12-slim AS base    # not python:3-slim, not python:latest
```

### Gap 2: Dev has extra packages that mask missing prod deps

A dev dependency (e.g., `ipython`) happens to install a shared library that
a prod dependency also needs. Works in dev, ImportError in prod.

```bash
# Catch it: run the production image locally before deploying
docker compose -f compose.yaml -f compose.prod.yaml build api
docker compose -f compose.yaml -f compose.prod.yaml run --rm api python -c "import myapp"
```

### Gap 3: Database versions differ

Dev runs MongoDB 7, prod runs MongoDB 6.0 (never upgraded). A query uses a
7.0-only aggregation operator.

```yaml
# Close it: pin the same version in compose.yaml and in prod infrastructure
image: mongo:7.0.4    # not mongo:7, not mongo:latest
```

### Gap 4: Environment variables present in dev but missing in prod

A feature works locally because `DEBUG=true` enables a fallback code path.
In prod, `DEBUG` is unset and the fallback doesn't run, revealing a bug.

```bash
# Catch it: use compose.prod.yaml to simulate prod env locally
docker compose -f compose.yaml -f compose.prod.yaml up -d
```

Pydantic Settings will raise a `ValidationError` immediately if a required
variable is missing — much better than a runtime failure under load.

### Gap 5: Volume-backed state in dev, clean slate in prod

Dev has stale data in a named volume from six months ago. A new migration
assumes a clean schema but the dev volume has old structure. Works in prod
(clean), breaks in dev.

```bash
# Fix it: periodically test against a fresh volume
docker compose down -v
docker compose up -d
```

---

## Putting It Together — Environment Summary

| | Dev | CI | Prod |
|---|---|---|---|
| Compose files | `compose.yaml` + `compose.override.yaml` | `compose.yaml` + `compose.ci.yaml` | `compose.yaml` + `compose.prod.yaml` |
| Build target | `development` | `test` | `production` (or pre-built image) |
| Source code | Bind mount | Baked in | Baked in |
| Database volumes | Named (persistent) | `tmpfs` (ephemeral) | Named (persistent + backups) |
| DB ports on host | ✅ (for inspection) | ❌ | ❌ |
| Hot reload | ✅ | ❌ | ❌ |
| Monitoring profile | Optional | ❌ | Optional |
| Secrets source | `secrets/*.txt` files | CI environment vars | Docker secrets or vault |
| Image source | Built locally | Built locally | Pulled from registry |

---

## Practical Exercise

1. **Write `compose.ci.yaml`** using `tmpfs` volumes for all databases and a
   `test` service that runs `pytest`. Verify it works locally:
   ```bash
   docker compose -f compose.yaml -f compose.ci.yaml run --rm test
   ```

2. **Write `compose.prod.yaml`** that references pre-built images from a
   registry (use a placeholder tag like `myapp-api:latest`), sets resource
   limits, and removes host port bindings from databases.

3. **Add the monitoring profile** to `compose.yaml` with Prometheus and
   Grafana. Confirm that `docker compose up -d` starts cleanly without them,
   and `docker compose --profile monitoring up -d` starts the full stack.

4. **Simulate a parity gap:** start the prod config locally with
   `docker compose -f compose.yaml -f compose.prod.yaml up -d`. Remove one
   required environment variable from `compose.prod.yaml` and observe how
   Pydantic Settings fails at startup.

5. **Write the GitHub Actions workflow** (or the equivalent for your CI
   system) that runs the test suite on every push to `main`.

**Stretch goal:** Add a `COMPOSE_PROFILES` variable to your `.env` file and
document in `.env.example` which profiles are available and what they enable.

<details>
<summary>Hint — tmpfs volumes</summary>

```yaml
services:
  db:
    volumes:
      - type: tmpfs
        target: /data/db
```

The `type: tmpfs` syntax requires the long-form volume entry (a map with
`type` and `target` keys) rather than the short-form `- name:/path` string.

</details>

<details>
<summary>Hint — --exit-code-from in CI</summary>

```bash
docker compose \
  -f compose.yaml \
  -f compose.ci.yaml \
  up migrate --exit-code-from migrate
```

This starts `migrate` and all its dependencies, waits for `migrate` to exit,
and returns its exit code. If `alembic upgrade head` fails, the shell command
fails, and the CI step fails. No polling or custom scripts needed.

</details>

---

## Key Takeaways

- **The base file is the prod description**, not a dev file with things removed.
  Overrides layer on top; they never subtract from the base.
- **Three-file strategy:** `compose.yaml` (base) + `compose.override.yaml`
  (dev, auto-loaded) + `compose.prod.yaml` (prod, explicit).
- **CI uses `tmpfs` volumes** for databases — ephemeral, fast, no cleanup
  needed between runs.
- **`docker compose config`** is always your first debugging tool when
  multi-file merges behave unexpectedly.
- **`--exit-code-from`** gives CI clean pass/fail signals from one-shot
  services like migration containers.
- **Profiles** compose cleanly with multi-file overrides — you can run
  `--profile monitoring` with any combination of `-f` flags.
- Parity gaps are cheapest to find locally. Simulate prod with
  `compose.prod.yaml` before pushing.

---

## Further Reading

- [Compose multiple files reference](https://docs.docker.com/compose/multiple-compose-files/)
- [Twelve-Factor App: Dev/prod parity](https://12factor.net/dev-prod-parity)
- [GitHub Actions: Docker Build Push](https://docs.docker.com/build/ci/github-actions/)
- [Prometheus FastAPI Instrumentator](https://github.com/trallnag/prometheus-fastapi-instrumentator)
- [tmpfs mounts](https://docs.docker.com/storage/tmpfs/)

---

## Next Module

Module 7 applies everything to AI and agent-specific stack patterns: Compose
for Argos-style orchestration, running Ollama as a local LLM service, wiring
Qdrant alongside BGE-M3, and patterns for managing agent worker pools.
