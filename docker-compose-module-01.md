---
module: 1
title: "Mental Model & Core Concepts"
duration: "½ day (~3–4 hours)"
prerequisites: "Comfortable with Docker CLI basics (pull, run, build, exec)"
---

# Module 1: Mental Model & Core Concepts

## Introduction

If you've ever started a FastAPI app, then in a separate terminal launched MongoDB,
then remembered you need Redis, then wondered how to wire them together without
hardcoding `localhost` — Docker Compose solves exactly that problem.

Compose doesn't replace Docker. It orchestrates Docker. A single `compose.yaml`
file becomes the canonical description of your entire local stack: what runs,
how it's configured, how services find each other, and what data persists between
restarts. One command brings everything up. One command tears it down cleanly.

This module builds the mental model you'll need before touching anything advanced.

---

## Learning Objectives

By the end of this module you will be able to:

- Explain what Compose adds over raw `docker run` commands
- Read and write a `compose.yaml` from scratch
- Use the core CLI commands confidently
- Manage environment variables and `.env` files correctly
- Use profiles to selectively start services

---

## 1. What Compose Adds Over Raw Docker

Before Compose, running a multi-service stack meant a shell script like this:

```bash
# the old way — don't do this
docker network create myapp-net

docker run -d \
  --name mongodb \
  --network myapp-net \
  -v mongo-data:/data/db \
  -e MONGO_INITDB_ROOT_USERNAME=admin \
  -e MONGO_INITDB_ROOT_PASSWORD=secret \
  mongo:7

docker run -d \
  --name api \
  --network myapp-net \
  -p 8000:8000 \
  -e MONGO_URI=mongodb://admin:secret@mongodb:27017 \
  myapp:latest
```

This works, but it has real problems: order matters, cleanup is manual, config
is scattered, and adding a third service doubles the complexity.

Compose gives you four things that shell scripts can't:

| Problem | Compose Solution |
|---|---|
| Scattered config | Single `compose.yaml` as source of truth |
| Manual startup order | `depends_on` + `healthcheck` |
| Teardown is painful | `docker compose down` removes everything |
| Networking is manual | Automatic shared network, DNS by service name |

The mental shift: stop thinking about individual containers and start thinking
about **services**. A service is a named, configured, networked unit. Compose
manages the lifecycle of all of them together.

---

## 2. `compose.yaml` Structure

The file has four top-level keys you'll use constantly:

```yaml
# compose.yaml — annotated skeleton

services:       # the containers you want to run
  api:
    image: myapp:latest
    ports:
      - "8000:8000"

  db:
    image: mongo:7

networks:       # custom networks (optional — a default is created automatically)
  backend:
    driver: bridge

volumes:        # named persistent volumes
  mongo-data:

configs:        # (advanced) inject config files into containers
secrets:        # (advanced) manage sensitive values
```

> **Note on filename:** Docker now prefers `compose.yaml` over the older
> `docker-compose.yml`. Both work, but use `compose.yaml` for new projects.

### A minimal real example

Here's a FastAPI + MongoDB stack that actually runs:

```yaml
# compose.yaml

services:
  api:
    build: .                          # build from Dockerfile in current dir
    ports:
      - "8000:8000"
    environment:
      MONGO_URI: mongodb://db:27017   # "db" resolves via Compose DNS
    depends_on:
      db:
        condition: service_healthy    # wait for the healthcheck to pass

  db:
    image: mongo:7
    volumes:
      - mongo-data:/data/db
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  mongo-data:
```

Notice `mongodb://db:27017` — `db` is the service name, and Compose automatically
creates DNS so services can find each other by name. No IP addresses, no
hardcoded hostnames.

---

## 3. Core CLI Commands

You'll use these every day. Learn the flags, not just the commands.

### `docker compose up`

```bash
# Start everything, stream logs to terminal
docker compose up

# Start in background (detached)
docker compose up -d

# Rebuild images before starting (important after code changes)
docker compose up -d --build

# Start only specific services
docker compose up -d api
```

### `docker compose down`

```bash
# Stop and remove containers + networks (volumes are preserved)
docker compose down

# Also remove named volumes (destructive — wipes database data)
docker compose down -v

# Also remove built images
docker compose down --rmi local
```

> **Rule of thumb:** use `down` during normal dev. Use `down -v` only when you
> want a clean slate (e.g., testing a fresh migration).

### `docker compose logs`

```bash
# Tail all service logs
docker compose logs -f

# Tail a specific service
docker compose logs -f api

# Show last 50 lines then follow
docker compose logs -f --tail=50 api
```

### `docker compose exec`

Run a command inside a running container — equivalent to `docker exec` but
using service names instead of container IDs.

```bash
# Open a shell in the api container
docker compose exec api bash

# Run a one-off command
docker compose exec db mongosh

# Run a migration script inside your app
docker compose exec api python manage.py migrate
```

### `docker compose ps`

```bash
# See status of all services
docker compose ps

# Example output:
# NAME         IMAGE       COMMAND       STATUS          PORTS
# myapp-api-1  myapp:dev   "uvicorn..."  Up 2 minutes    0.0.0.0:8000->8000/tcp
# myapp-db-1   mongo:7     "docker-e..."  Up 2 minutes
```

### `docker compose build`

```bash
# Build (or rebuild) images without starting them
docker compose build

# Rebuild a specific service, no cache
docker compose build --no-cache api
```

### Quick reference

| Command | What it does |
|---|---|
| `up -d` | Start all services in background |
| `up -d --build` | Rebuild then start |
| `down` | Stop and remove containers |
| `down -v` | Stop and wipe volumes too |
| `logs -f api` | Tail a service's logs |
| `exec api bash` | Shell into a running service |
| `ps` | See what's running |
| `build --no-cache` | Force full image rebuild |

---

## 4. Environment Variables and `.env` Files

Configuration that changes between environments (dev/staging/prod) should never
be hardcoded in `compose.yaml`. Compose has three mechanisms, and knowing when
to use each matters.

### The `.env` file (automatic)

Create a `.env` file next to `compose.yaml`:

```bash
# .env
MONGO_USER=admin
MONGO_PASS=secret
API_PORT=8000
```

Compose loads this file automatically. Reference variables with `${}` syntax:

```yaml
# compose.yaml
services:
  api:
    ports:
      - "${API_PORT}:8000"

  db:
    image: mongo:7
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${MONGO_USER}
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_PASS}
```

> **Always add `.env` to `.gitignore`.** Commit a `.env.example` with dummy
> values instead so teammates know what variables are required.

### `environment:` vs `env_file:`

```yaml
services:
  api:
    # Option A: inline — good for a few variables
    environment:
      DEBUG: "true"
      LOG_LEVEL: info
      DATABASE_URL: ${DATABASE_URL}   # pulls from .env

    # Option B: load from a file — good for many variables
    env_file:
      - .env
      - .env.local     # optional overrides, also gitignored
```

### The `--env-file` flag

Override which `.env` file is loaded at runtime:

```bash
# Use a staging-specific env file
docker compose --env-file .env.staging up -d
```

This is the cleanest way to support multiple environments without maintaining
multiple `compose.yaml` files.

### Variable precedence (highest to lowest)

1. Shell environment (`export FOO=bar`)
2. `--env-file` flag
3. `.env` file in the project directory
4. Defaults defined in `compose.yaml` with `${VAR:-default}`

```yaml
# Default value syntax
environment:
  LOG_LEVEL: ${LOG_LEVEL:-info}     # uses "info" if LOG_LEVEL is not set
  PORT: ${PORT:-8000}
```

---

## 5. Profiles

Profiles let you define optional services that only start when explicitly
requested. This is ideal for services you don't always need: monitoring stacks,
debug tools, load test harnesses, or alternative database backends.

```yaml
services:
  api:
    build: .
    ports:
      - "8000:8000"

  db:
    image: mongo:7

  # Only starts when the "monitoring" profile is active
  prometheus:
    image: prom/prometheus
    profiles:
      - monitoring
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana
    profiles:
      - monitoring
    ports:
      - "3000:3000"

  # Only starts when the "debug" profile is active
  mongo-express:
    image: mongo-express
    profiles:
      - debug
    ports:
      - "8081:8081"
    environment:
      ME_CONFIG_MONGODB_URL: mongodb://db:27017
```

```bash
# Normal dev — api and db only
docker compose up -d

# Dev with monitoring stack
docker compose --profile monitoring up -d

# Dev with mongo-express UI
docker compose --profile debug up -d

# Both profiles at once
docker compose --profile monitoring --profile debug up -d
```

You can also set the active profiles via environment variable:

```bash
COMPOSE_PROFILES=monitoring,debug docker compose up -d
```

---

## Practical Exercise

Build a working `compose.yaml` for the following stack:

- **FastAPI service** — built from a local `Dockerfile`, exposed on port 8000
- **MongoDB** — persisted via a named volume, with a healthcheck
- **Mongo Express** — database UI, but only active under a `debug` profile

Requirements:
1. The FastAPI service must not start until MongoDB passes its healthcheck
2. All credentials must come from a `.env` file (not hardcoded)
3. Services communicate using service-name DNS (no IP addresses)
4. Running `docker compose up -d` should start only FastAPI and MongoDB

**Stretch goal:** Add a `.env.example` file with placeholder values and a
`README.md` section explaining how to use the stack.

<details>
<summary>Hint</summary>

- `depends_on` with `condition: service_healthy` handles startup ordering
- The `healthcheck` on MongoDB should use `mongosh --eval "db.adminCommand('ping')"`
- Mongo Express needs `ME_CONFIG_MONGODB_URL` pointing to the `db` service name
- Give Mongo Express `profiles: [debug]`

</details>

---

## Key Takeaways

- Compose manages the **lifecycle of a group of services** as a unit — not individual containers
- `compose.yaml` is the single source of truth for your local stack
- Services find each other by **service name** via automatic DNS — never hardcode IPs
- Use `.env` for secrets and environment-specific config; **never commit `.env`**
- `down` removes containers; `down -v` also removes volumes — know the difference
- **Profiles** cleanly separate optional services from your core stack

---

## Further Reading

- [Docker Compose file reference](https://docs.docker.com/compose/compose-file/)
- [Compose networking deep dive](https://docs.docker.com/compose/networking/)
- [Healthcheck documentation](https://docs.docker.com/compose/compose-file/05-services/#healthcheck)

---

## Next Module

Module 2 covers writing production-quality `Dockerfile`s optimized for Compose:
multi-stage builds, cache layer ordering, and the difference between `build:` and
`image:` and when each is the right choice.
