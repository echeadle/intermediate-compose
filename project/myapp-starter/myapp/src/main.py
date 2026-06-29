# src/main.py

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
import os


# ── Database connection ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the MongoDB connection on startup, close it on shutdown."""
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    app.state.client = AsyncIOMotorClient(mongo_uri)
    app.state.db = app.state.client["myapp"]
    yield
    app.state.client.close()


app = FastAPI(title="myapp", lifespan=lifespan)


# ── Models ─────────────────────────────────────────────────────────────────

class Item(BaseModel):
    name: str
    description: str = ""


class ItemResponse(BaseModel):
    id: str
    name: str
    description: str
    created_at: str


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Used by Docker healthchecks and load balancers."""
    return {"status": "ok"}


@app.get("/items", response_model=list[ItemResponse])
async def list_items():
    """Return all items from MongoDB."""
    items = []
    async for doc in app.state.db.items.find():
        items.append(ItemResponse(
            id=str(doc["_id"]),
            name=doc["name"],
            description=doc.get("description", ""),
            created_at=doc["created_at"],
        ))
    return items


@app.post("/items", response_model=ItemResponse, status_code=201)
async def create_item(item: Item):
    """Insert a new item into MongoDB."""
    doc = {
        "name": item.name,
        "description": item.description,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await app.state.db.items.insert_one(doc)
    return ItemResponse(
        id=str(result.inserted_id),
        **item.model_dump(),
        created_at=doc["created_at"],
    )
