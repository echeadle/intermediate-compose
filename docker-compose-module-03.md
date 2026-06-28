---
module: 3
title: "Networking & Communication"
duration: "½ day (~3–4 hours)"
prerequisites: "Modules 1 and 2 complete"
---

# Module 3: Networking & Communication

## Introduction

In Module 1 you saw that services talk to each other using their service name
as the hostname — `mongodb://db:27017` instead of `mongodb://172.18.0.3:27017`.
That "just works," and most of the time you never need to think about it.

Until you need to.

When should your reverse proxy be able to reach the API but not the database
directly? When should a monitoring service see everything but remain unreachable
from the outside? How do you connect two separate Compose projects so their
services can communicate?

This module explains the networking model that makes service-name DNS work,
then shows you how to shape it deliberately — isolating services that should be
isolated, bridging services that need to communicate, and controlling what the
host machine can reach.

---

## Learning Objectives

By the end of this module you will be able to:

- Explain how Compose's default bridge network and DNS work
- Trace the path of a request from one service to another
- Design a multi-network topology that reflects real security boundaries
- Control port exposure correctly (`ports:` vs `expose:`)
- Connect services across separate Compose projects using external networks

---

## 1. The Default Network

When you run `docker compose up`, Compose creates a single bridge network named
after your project directory:

```
myapp_default
```

Every service defined in `compose.yaml` is automatically attached to this
network. That's the entire reason services can reach each other by name — they
share a network namespace with an embedded DNS resolver.

```bash
# See the network Compose created
docker network ls

# NETWORK ID     NAME              DRIVER    SCOPE
# a1b2c3d4e5f6   myapp_default     bridge    local
```

You can inspect it to see which containers are attached:

```bash
docker network inspect myapp_default
```

The output shows each container's IP on the network and the aliases it
responds to.

### What the DNS resolver does

Each container on the network runs a tiny DNS resolver. When `api` sends a
request to `db:27017`, here's what happens:

```
api container
  └─ resolves "db" via embedded DNS
       └─ DNS returns the IP of the db container on the shared network
            └─ TCP connection to that IP:27017
```

The resolver knows about service names because Compose registers each service
when it joins the network. Aliases, container names, and service names all
resolve. This is why you never need to know or hardcode container IP addresses.

### Project name and network name

The default network name is `{project_name}_default`. The project name defaults
to the directory name, but you can override it:

```bash
# Override project name at runtime
docker compose -p myapp up -d

# Or set it permanently in compose.yaml
name: myapp
```

This matters when you have multiple Compose files in the same directory, or
when you want predictable network names for cross-project connections (covered
in Section 4).

---

## 2. Custom Networks

The default single network is fine for simple stacks. For anything with real
security boundaries — a public-facing API, an internal database tier, a
monitoring layer — you want multiple networks with deliberate membership.

### Defining custom networks

```yaml
# compose.yaml

networks:
  frontend:     # reachable from outside (nginx, api)
    driver: bridge
  backend:      # internal only (api <-> db, api <-> redis)
    driver: bridge
  monitoring:   # observability layer (prometheus, grafana)
    driver: bridge
```

### Attaching services to networks

```yaml
services:
  nginx:
    image: nginx:alpine
    networks:
      - frontend          # public-facing — on frontend only
    ports:
      - "80:80"
      - "443:443"

  api:
    build: .
    networks:
      - frontend          # nginx can reach it
      - backend           # can reach db and redis
    # no ports: — not directly reachable from the host

  db:
    image: mongo:7.0.4
    networks:
      - backend           # only api can reach it
    # no ports: — completely internal

  redis:
    image: redis:7-alpine
    networks:
      - backend

  prometheus:
    image: prom/prometheus
    networks:
      - monitoring
      - backend           # needs to scrape api metrics endpoint
    profiles:
      - monitoring

  grafana:
    image: grafana/grafana
    networks:
      - frontend          # browser-accessible
      - monitoring        # can query prometheus
    profiles:
      - monitoring
    ports:
      - "3000:3000"
```

### What this topology enforces

```
Internet
  │
  ▼
[nginx] ──── frontend network ──── [grafana]
               │
             [api]
               │
           backend network
            │       │
          [db]   [redis]

monitoring network
  │
[prometheus] ──── can scrape [api] (shared backend network)
  │
[grafana]    ──── queries [prometheus] (shared monitoring network)
```

`db` is unreachable from `nginx` — they share no network. `api` can reach
both `nginx` (as a downstream) and `db` (as a dependency) because it sits
on two networks simultaneously.

This isn't theoretical security — it's defense in depth. A compromised nginx
container cannot directly query your database.

### Network-level aliases

A service can have a different name on different networks:

```yaml
services:
  api:
    networks:
      frontend:
        aliases:
          - app             # reachable as "app" on the frontend network
      backend:
        aliases:
          - api-internal    # reachable as "api-internal" on the backend network
```

Aliases are useful when migrating service names without breaking other services
that reference the old name.

---

## 3. `ports:` vs `expose:` — Controlling Host Access

These two keys are frequently confused. They do very different things.

### `ports:` — binds to the host

```yaml
services:
  api:
    ports:
      - "8000:8000"         # host:container
```

This publishes the container's port 8000 to the *host machine* on port 8000.
Your browser, curl, and any external tool can now reach it at `localhost:8000`.

Port mapping formats:

```yaml
ports:
  - "8000:8000"             # bind to all interfaces (0.0.0.0)
  - "127.0.0.1:8000:8000"  # bind to loopback only — more secure in dev
  - "8000"                  # random host port (Compose assigns it)
  - "8000-8010:8000-8010"  # port range
```

> **Use `127.0.0.1:` binding in development** for services you only need to
> access locally. It prevents other machines on your network from reaching
> your dev database.

### `expose:` — documents internal ports

```yaml
services:
  db:
    expose:
      - "27017"
```

`expose:` does almost nothing at runtime. The port is already reachable by
other services on the same network without it. Its purpose is documentation —
it signals to humans (and tooling) that this port is the service's interface.
It does **not** publish to the host.

### The practical rule

| You want | Use |
|---|---|
| Host machine (browser, curl) can reach the service | `ports:` |
| Only other Compose services can reach it | nothing (default) or `expose:` for docs |
| Explicitly block host access even if you change your mind | use custom networks and omit `ports:` |

Most internal services — databases, caches, internal APIs — should have no
`ports:` at all. Only the entry points to your stack (nginx, a public API, a
dashboard) need `ports:`.

---

## 4. Cross-Project Networking

Sometimes you have two separate `compose.yaml` projects that need to share
services. A common scenario: a shared infrastructure stack (databases, message
queues) and multiple application stacks that all connect to it.

### Step 1: Create a named external network in the infrastructure project

```yaml
# infra/compose.yaml
name: infra

networks:
  shared:
    driver: bridge

services:
  db:
    image: mongo:7.0.4
    networks:
      - shared
    volumes:
      - mongo-data:/data/db

  redis:
    image: redis:7-alpine
    networks:
      - shared

volumes:
  mongo-data:
```

After `docker compose -p infra up -d`, a network named `infra_shared` exists.

### Step 2: Join that network from another project

```yaml
# app/compose.yaml
name: myapp

networks:
  # declare the external network — Compose will not try to create it
  infra_shared:
    external: true

services:
  api:
    build: .
    networks:
      - infra_shared    # joins the already-running infra network
    environment:
      MONGO_URI: mongodb://db:27017    # "db" resolves on the shared network
```

The `api` container can now resolve `db` and `redis` by name because all three
share the `infra_shared` network.

> **Order matters:** the infra stack must be running before the app stack starts.
> The external network must exist before Compose tries to attach to it.

### When to use this pattern

- Shared local development infrastructure (one db instance for multiple services)
- Argos-style orchestration where an agent runner needs to connect to databases
  managed by a separate Compose project
- Gradually migrating a monolith to microservices — keep the database stack
  separate and stable while you iterate on services

---

## 5. Useful Network Debugging Commands

Networking issues are silent — a misconfigured network means a connection
refused, which looks identical to a service being down. These commands help
you see what's actually happening.

### Inspect from inside a container

```bash
# Shell into a running service
docker compose exec api bash

# Test DNS resolution
nslookup db
# Server:    127.0.0.11   ← embedded DNS resolver
# Address:   127.0.0.11#53
# Name:      db
# Address:   172.18.0.3

# Test reachability
curl http://db:27017
ping db

# Install network tools if not present (Debian/Ubuntu based)
apt-get install -y iputils-ping dnsutils curl
```

### Inspect from the host

```bash
# List all networks
docker network ls

# See which containers are on a network and their IPs
docker network inspect myapp_backend

# See what networks a container is on
docker inspect myapp-api-1 | grep -A 20 '"Networks"'
```

### One-shot debugging container

If your service image doesn't have networking tools, run a temporary container
on the same network:

```bash
# Attach a temporary Alpine container to your network
docker run --rm -it \
  --network myapp_backend \
  alpine sh

# Inside: test DNS and connectivity
nslookup db
wget -qO- http://api:8000/health
```

---

## Putting It Together — Full Multi-Network Stack

A realistic production-like topology with correct network segmentation:

```yaml
# compose.yaml
name: myapp

networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge

services:
  nginx:
    image: nginx:1.27-alpine
    networks:
      - frontend
    ports:
      - "127.0.0.1:80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      api:
        condition: service_healthy

  api:
    build:
      context: .
      target: development
    networks:
      - frontend
      - backend
    environment:
      MONGO_URI: mongodb://${MONGO_USER}:${MONGO_PASS}@db:27017
      REDIS_URL: redis://redis:6379
    volumes:
      - ./src:/app/src
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy

  db:
    image: mongo:7.0.4
    networks:
      - backend           # not reachable from nginx
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

  redis:
    image: redis:7-alpine
    networks:
      - backend           # not reachable from nginx
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3

volumes:
  mongo-data:
```

Connectivity matrix:

| From → To | nginx | api | db | redis |
|---|---|---|---|---|
| nginx | — | ✅ | ❌ | ❌ |
| api | ✅ | — | ✅ | ✅ |
| db | ❌ | ✅ | — | ❌ |
| redis | ❌ | ✅ | ❌ | — |
| **host** | ✅ :80 | ❌ | ❌ | ❌ |

The host can only reach nginx on port 80. Everything else is internal.

---

## Practical Exercise

Build on the stack from Module 2 and add proper network segmentation:

1. **Create two networks:** `frontend` and `backend`.

2. **Attach services correctly:**
   - nginx (or just the API for now) on `frontend`
   - API on both `frontend` and `backend`
   - MongoDB on `backend` only

3. **Remove `ports:` from MongoDB entirely.** Confirm you can no longer
   connect to it directly from the host (`mongosh --port 27017` should fail),
   but that the API can still reach it.

4. **Bind the API's port to loopback only:** change `"8000:8000"` to
   `"127.0.0.1:8000:8000"`.

5. **Verify DNS resolution** by shelling into the API container and running
   `nslookup db`. Confirm the hostname resolves to an internal IP.

**Stretch goal:** Create a separate `infra/compose.yaml` that runs MongoDB
alone on an external network named `infra_shared`. Update your main
`compose.yaml` to join that external network and remove the local `db` service.
Confirm the API can still reach MongoDB by service name.

<details>
<summary>Hint — removing host access to MongoDB</summary>

Simply remove the `ports:` key from the `db` service entirely. Services on
the same Compose network can always reach each other — `ports:` is only for
host access.

</details>

<details>
<summary>Hint — external networks</summary>

In the consuming `compose.yaml`, declare the network with `external: true`:

```yaml
networks:
  infra_shared:
    external: true
```

Then attach services to it normally. The network must already exist (i.e., the
infra stack must be running) before you run `docker compose up`.

</details>

---

## Key Takeaways

- Compose creates a **default bridge network** automatically; all services join
  it and can reach each other by service name via an embedded DNS resolver.
- **Custom networks** let you enforce security boundaries — a service only sees
  the other services on its shared networks.
- A service can join **multiple networks** simultaneously, making it the bridge
  between tiers (e.g., an API that faces both nginx and the database).
- **`ports:`** binds to the host; **`expose:`** is documentation only.
  Internal services should have neither.
- Bind host ports to `127.0.0.1` in development to avoid accidental external
  exposure.
- **External networks** enable cross-project communication — useful for shared
  infrastructure or multi-repo microservice setups.
- When debugging, `docker network inspect` and a one-shot Alpine container are
  your fastest tools.

---

## Further Reading

- [Compose networking reference](https://docs.docker.com/compose/networking/)
- [Docker bridge network driver](https://docs.docker.com/network/drivers/bridge/)
- [Docker embedded DNS](https://docs.docker.com/network/drivers/bridge/#dns-services)

---

## Next Module

Module 4 covers data and persistence: the difference between named volumes and
bind mounts, strategies for database volumes, sharing data between services,
and handling secrets without putting them in environment variables.
