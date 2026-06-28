---
module: 4
title: "Data & Persistence"
duration: "½ day (~3–4 hours)"
prerequisites: "Modules 1–3 complete"
---

# Module 4: Data & Persistence

## Introduction

Containers are ephemeral by design. When a container stops, anything written
inside it disappears — unless you've explicitly told Docker where to persist it.

This is the right default. It means containers are reproducible and stateless.
But your database is not stateless. Your uploaded files are not stateless. Your
vector embeddings definitely are not stateless.

Docker gives you two mechanisms for persistence: **named volumes** (Docker
manages the storage location) and **bind mounts** (you specify an exact path on
the host). Choosing the wrong one for the job creates subtle problems — slow
writes, data loss on rebuild, or secrets leaking into source control.

This module covers both mechanisms in depth, then addresses the related problem
of secrets: how to get credentials into containers without hardcoding them in
`compose.yaml` or committing them to git.

---

## Learning Objectives

By the end of this module you will be able to:

- Explain the difference between named volumes and bind mounts and choose the
  right one for each use case
- Configure durable, backup-friendly volumes for MongoDB, pgvector, and
  ChromaDB
- Share data between services using volumes
- Manage secrets correctly across three levels of sensitivity

---

## 1. Named Volumes vs Bind Mounts

### Named volumes

Docker creates and manages the storage location. You refer to it by name.

```yaml
services:
  db:
    image: mongo:7.0.4
    volumes:
      - mongo-data:/data/db    # named volume : container path

volumes:
  mongo-data:                  # declare it at the top level
```

Docker stores the data somewhere under `/var/lib/docker/volumes/` on the host
(or a Docker Desktop VM equivalent). You don't see or manage that path directly.

**Use named volumes when:**
- The data belongs to a service, not to you (databases, caches)
- You want Docker to own the lifecycle — create, preserve across `down`,
  delete only on `down -v`
- Portability matters — the same `compose.yaml` works identically on any host
- You need volume drivers for remote storage (NFS, S3-backed volumes)

### Bind mounts

You specify a host path that maps directly into the container.

```yaml
services:
  api:
    volumes:
      - ./src:/app/src           # host path : container path
      - ./config:/app/config:ro  # :ro = read-only inside container
```

The container sees the host filesystem path live. Changes on either side
are immediately visible on the other.

**Use bind mounts when:**
- You're developing and want live code reload (your source files)
- You need to inject config files from the host (nginx.conf, prometheus.yml)
- You want to inspect or edit the data directly with host tools
- The files are already in your project directory

### Side-by-side comparison

| | Named Volume | Bind Mount |
|---|---|---|
| Storage location | Docker manages | You specify |
| Survives `down` | ✅ | ✅ (it's just a host path) |
| Survives `down -v` | ❌ deleted | ✅ (host files untouched) |
| Works on any OS | ✅ | ⚠️ path must exist |
| Performance on Linux | ✅ native | ✅ native |
| Performance on Mac/Win | ✅ good | ⚠️ can be slow (VM overhead) |
| Inspect with host tools | ❌ awkward | ✅ directly |
| Best for | databases, caches | source code, config files |

### The performance caveat on macOS

On macOS and Windows, Docker runs in a Linux VM. Bind mounts cross a VM
boundary — every filesystem operation goes through a translation layer. For
source code this is noticeable but acceptable. For a database doing thousands
of writes per second, it's a serious bottleneck.

This is why you should **always use named volumes for databases**, even on
macOS where the named volume lives entirely inside the VM and never crosses
the translation layer.

---

## 2. Volume Configuration Options

### Read-only mounts

Prevent the container from writing to a bind mount:

```yaml
services:
  api:
    volumes:
      - ./config/settings.toml:/app/config/settings.toml:ro
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
```

Useful for config files you want to be immutable inside the container — the
app can read the config but can't accidentally overwrite it.

### Volume driver options

For named volumes, you can configure the storage driver:

```yaml
volumes:
  pgdata:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/fast-ssd/pgdata   # pin to a specific host path
```

This hybrid lets you use named volume syntax in `compose.yaml` while pinning
the actual storage location — useful when you have a fast NVMe drive you want
Postgres to use specifically.

### Initializing a volume with data

Some database images support initialization scripts placed in a special
directory:

```yaml
services:
  db:
    image: mongo:7.0.4
    volumes:
      - mongo-data:/data/db
      - ./init-scripts:/docker-entrypoint-initdb.d:ro   # runs on first start
```

Scripts in `/docker-entrypoint-initdb.d/` run only when the volume is empty
(first start). On subsequent starts they're ignored — the database is already
initialized.

---

## 3. Volume Strategies for Your Stack

### MongoDB

```yaml
services:
  db:
    image: mongo:7.0.4
    volumes:
      - mongo-data:/data/db          # database files
      - mongo-config:/data/configdb  # replica set config (if using replication)
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${MONGO_USER}
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_PASS}
      MONGO_INITDB_DATABASE: ${MONGO_DB}

volumes:
  mongo-data:
  mongo-config:
```

**Backup pattern:**

```bash
# Dump to host via a temporary container
docker compose exec db \
  mongodump \
    --username ${MONGO_USER} \
    --password ${MONGO_PASS} \
    --out /tmp/backup

docker compose cp db:/tmp/backup ./backups/$(date +%Y%m%d)
```

### PostgreSQL + pgvector

```yaml
services:
  pgvector:
    image: pgvector/pgvector:pg16    # official pgvector image
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init-scripts/01-extensions.sql:/docker-entrypoint-initdb.d/01-extensions.sql:ro
    environment:
      POSTGRES_USER: ${PG_USER}
      POSTGRES_PASSWORD: ${PG_PASS}
      POSTGRES_DB: ${PG_DB}
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "${PG_USER}"]
      interval: 5s
      timeout: 3s
      retries: 5
```

```sql
-- init-scripts/01-extensions.sql
-- Runs once on first start to enable the pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;
```

**Migration pattern** (pairs with Module 2's `service_completed_successfully`):

```yaml
services:
  migrate:
    build: .
    command: ["python", "-m", "alembic", "upgrade", "head"]
    depends_on:
      pgvector:
        condition: service_healthy

  api:
    depends_on:
      migrate:
        condition: service_completed_successfully
```

### ChromaDB

```yaml
services:
  chroma:
    image: chromadb/chroma:latest
    volumes:
      - chroma-data:/chroma/chroma
    environment:
      ANONYMIZED_TELEMETRY: "false"
      ALLOW_RESET: "false"     # prevents accidental wipes in prod
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/heartbeat"]
      interval: 10s
      timeout: 5s
      retries: 3

volumes:
  chroma-data:
```

### Qdrant

```yaml
services:
  qdrant:
    image: qdrant/qdrant:latest
    volumes:
      - qdrant-data:/qdrant/storage
      - ./qdrant-config.yaml:/qdrant/config/production.yaml:ro
    ports:
      - "127.0.0.1:6333:6333"   # REST API
      - "127.0.0.1:6334:6334"   # gRPC
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/readyz"]
      interval: 10s
      timeout: 5s
      retries: 3

volumes:
  qdrant-data:
```

---

## 4. Sharing Volumes Between Services

Sometimes two services need access to the same data. The most common cases:
a web server and an application server sharing uploaded files, a worker and an
API sharing a job queue directory, or an ingest pipeline writing to a path that
a search service reads from.

### Pattern 1: Shared named volume

Both services mount the same named volume at their respective paths:

```yaml
services:
  ingest:
    build: .
    command: ["python", "-m", "ingest.pipeline"]
    volumes:
      - corpus-data:/app/corpus      # writes processed docs here

  api:
    build: .
    volumes:
      - corpus-data:/app/corpus:ro   # reads from the same volume, read-only

volumes:
  corpus-data:
```

> **Use `:ro` on the consuming side** when only one service should write.
> It prevents accidental writes and makes the data flow explicit.

### Pattern 2: Sidecar for export

A sidecar container reads from a volume and does something with the data
(backup, export, sync) without being part of the main request path:

```yaml
services:
  db:
    image: mongo:7.0.4
    volumes:
      - mongo-data:/data/db

  backup-sidecar:
    image: mongo:7.0.4
    volumes:
      - mongo-data:/data/db:ro        # same volume, read-only
      - ./backups:/backups
    command: >
      sh -c "mongodump --uri=mongodb://db:27017 --out=/backups/$(date +%Y%m%d)"
    profiles:
      - backup                        # only runs when explicitly invoked
    depends_on:
      - db
```

```bash
# Run a backup on demand
docker compose --profile backup run --rm backup-sidecar
```

### Pattern 3: Init container populating a volume

A one-shot container writes data into a volume that the main service then reads:

```yaml
services:
  model-downloader:
    image: python:3.12-slim
    volumes:
      - model-cache:/models
    command: >
      python -c "
      import urllib.request
      urllib.request.urlretrieve(
        'https://example.com/model.gguf',
        '/models/model.gguf'
      )
      "

  inference:
    image: ollama/ollama
    volumes:
      - model-cache:/models:ro
    depends_on:
      model-downloader:
        condition: service_completed_successfully

volumes:
  model-cache:
```

---

## 5. Secrets Management

Secrets in Compose exist on a spectrum. Match the tool to the sensitivity level.

### Level 1: `.env` file (low sensitivity — dev only)

Good for: API keys for local dev services, database credentials on a laptop,
anything that never leaves your machine.

```bash
# .env
MONGO_USER=admin
MONGO_PASS=devpassword123
OPENAI_API_KEY=sk-...
```

```yaml
services:
  api:
    environment:
      MONGO_URI: mongodb://${MONGO_USER}:${MONGO_PASS}@db:27017
      OPENAI_API_KEY: ${OPENAI_API_KEY}
```

Rules:
- **Always in `.gitignore`**
- **Always have a `.env.example`** with placeholder values committed to git
- Never use in CI/CD pipelines or production deployments

### Level 2: External environment injection (medium sensitivity — CI/CD)

The secret is never in any file. It's injected by the shell or a secrets
manager at runtime:

```bash
# In a CI/CD pipeline or deployment script
export MONGO_PASS="$(vault kv get -field=password secret/mongodb)"
docker compose up -d
```

Compose automatically picks up shell environment variables that match names
in `compose.yaml`. No file involved — the secret lives only in memory.

You can also pass them explicitly:

```bash
MONGO_PASS=hunter2 docker compose up -d
```

### Level 3: Docker secrets (high sensitivity — production Compose)

Docker has a first-class secrets system that mounts secrets as files inside
containers at `/run/secrets/<name>`. The secret value is never in an
environment variable (which can be leaked via `docker inspect`).

```yaml
# compose.yaml

secrets:
  mongo_password:
    file: ./secrets/mongo_password.txt   # dev: read from file
    # In production with Docker Swarm:
    # external: true                     # managed by Docker Swarm secrets

services:
  db:
    image: mongo:7.0.4
    secrets:
      - mongo_password
    environment:
      MONGO_INITDB_ROOT_USERNAME: admin
      # Point to the file, not a value:
      MONGO_INITDB_ROOT_PASSWORD_FILE: /run/secrets/mongo_password
```

```bash
# Create the secrets directory (gitignored)
mkdir -p secrets
echo "supersecretpassword" > secrets/mongo_password.txt
```

```
# .gitignore
secrets/
```

Your application reads the secret from the filesystem:

```python
# Reading a Docker secret in Python
from pathlib import Path

def get_secret(name: str) -> str:
    secret_path = Path(f"/run/secrets/{name}")
    if secret_path.exists():
        return secret_path.read_text().strip()
    # Fallback to environment variable for local dev without secrets
    import os
    return os.environ[name.upper()]
```

### Choosing the right level

| Scenario | Approach |
|---|---|
| Local dev, personal laptop | `.env` file |
| Shared dev environment | `.env` + `.env.example`, secrets in team vault |
| CI/CD pipeline | Environment injection from pipeline secrets |
| Production (single host) | Docker secrets via file |
| Production (cluster) | Docker Swarm / Kubernetes secrets |
| Third-party keys (OpenAI etc.) | Environment injection or Docker secrets |

### What never to do

```yaml
# ❌ Never hardcode secrets in compose.yaml
services:
  db:
    environment:
      MONGO_PASS: supersecretpassword   # committed to git, visible in docker inspect

# ❌ Never put secrets in a Dockerfile
ENV OPENAI_API_KEY=sk-abc123           # baked into every layer of the image
```

Both approaches bake secrets into artifacts that get committed or shared.
Even `docker inspect` can reveal environment variables to anyone with Docker
socket access on the host.

---

## Putting It Together — dev-rag Storage Stack

A complete storage layer for a RAG pipeline with multiple vector databases,
correct volume strategy, and proper secrets handling:

```yaml
# compose.yaml
name: dev-rag

secrets:
  pg_password:
    file: ./secrets/pg_password.txt

networks:
  backend:
    driver: bridge

services:
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

  chroma:
    image: chromadb/chroma:latest
    networks:
      - backend
    volumes:
      - chroma-data:/chroma/chroma
    environment:
      ANONYMIZED_TELEMETRY: "false"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/heartbeat"]
      interval: 10s
      timeout: 5s
      retries: 3

  qdrant:
    image: qdrant/qdrant:latest
    networks:
      - backend
    volumes:
      - qdrant-data:/qdrant/storage
    profiles:
      - qdrant             # optional — only when evaluating Qdrant
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/readyz"]
      interval: 10s
      timeout: 5s
      retries: 3

  corpus:
    image: busybox         # lightweight volume owner
    volumes:
      - corpus-data:/corpus
    command: ["sh", "-c", "chmod 777 /corpus && sleep infinity"]
    profiles:
      - tools              # utility service, not part of main stack

  ingest:
    build:
      context: .
      target: development
    networks:
      - backend
    volumes:
      - ./src:/app/src
      - corpus-data:/app/corpus
    environment:
      PG_DSN: postgresql://${PG_USER:-raguser}@pgvector:5432/${PG_DB:-ragdb}
      CHROMA_HOST: chroma
      CHROMA_PORT: "8000"
    depends_on:
      pgvector:
        condition: service_healthy
      chroma:
        condition: service_healthy

volumes:
  pgdata:
  chroma-data:
  qdrant-data:
  corpus-data:
```

---

## Practical Exercise

Extend your stack from Module 3 to add durable storage and secrets:

1. **Convert all credentials** to use Docker secrets. Create a `secrets/`
   directory (gitignored), write a `mongo_password.txt` into it, and
   reference it in both the `db` service and your API's connection string.

2. **Add a ChromaDB service** with a named volume. Confirm data persists
   across `docker compose down` / `up` cycles by inserting a document and
   checking it's still there after restart.

3. **Create a `.env.example`** that documents every variable your stack needs,
   with placeholder values and a comment explaining each one.

4. **Add a backup sidecar** under a `backup` profile that dumps MongoDB to a
   `./backups/` directory on the host. Verify it works with:
   ```bash
   docker compose --profile backup run --rm backup-sidecar
   ```

5. **Test volume isolation:** run `docker compose down` (no `-v`), then `up`
   again. Confirm your database data survived. Then run `docker compose down -v`
   and confirm the data is gone.

**Stretch goal:** Add a shared `corpus-data` volume between an `ingest` service
(write access) and a `search` service (read-only access). The `ingest` service
can be a simple Python script that writes a text file; the `search` service can
be a script that reads and prints it.

<details>
<summary>Hint — Docker secrets in dev</summary>

```bash
mkdir secrets
echo "mydevpassword" > secrets/mongo_password.txt
```

In `compose.yaml`:
```yaml
secrets:
  mongo_password:
    file: ./secrets/mongo_password.txt
```

Some MongoDB images support `MONGO_INITDB_ROOT_PASSWORD_FILE`. For your own
application, read from `/run/secrets/mongo_password` in Python.

</details>

<details>
<summary>Hint — testing persistence</summary>

```bash
docker compose up -d
docker compose exec db mongosh -u admin -p yourpassword \
  --eval "db.test.insertOne({hello: 'world'})"
docker compose down          # no -v
docker compose up -d
docker compose exec db mongosh -u admin -p yourpassword \
  --eval "db.test.find()"   # should still show the document
```

</details>

---

## Key Takeaways

- **Named volumes for databases, bind mounts for source code and config.**
  Mixing them up causes either data loss or performance problems.
- Named volumes **survive `down` but not `down -v`**. Know which you're running.
- On macOS/Windows, always use named volumes for databases — bind mounts cross
  a VM boundary and are slow for write-heavy workloads.
- **Initialization scripts** in `/docker-entrypoint-initdb.d/` run once on
  first start. Use them for extension setup (`CREATE EXTENSION vector`) and
  seed data.
- Match secret handling to sensitivity: `.env` for dev, environment injection
  for CI/CD, Docker secrets for production.
- **Secrets in `environment:` are visible via `docker inspect`.** Docker secrets
  mounted as files are not.
- A `corpus-data` shared volume with `:ro` on consumers makes data flow
  explicit and prevents accidental overwrites.

---

## Further Reading

- [Docker volumes reference](https://docs.docker.com/storage/volumes/)
- [Bind mounts](https://docs.docker.com/storage/bind-mounts/)
- [Docker Compose secrets](https://docs.docker.com/compose/use-secrets/)
- [pgvector Docker image](https://hub.docker.com/r/pgvector/pgvector)
- [ChromaDB Docker setup](https://docs.trychroma.com/production/containers/docker)

---

## Next Module

Module 5 brings Modules 1–4 together into a full Python stack: FastAPI +
MongoDB + Redis, with hot reload in development, migration containers, and
the complete `compose.override.yaml` pattern for layering dev config on top
of a prod-ready base.
