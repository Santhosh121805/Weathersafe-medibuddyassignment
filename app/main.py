

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import ask
from app.policy import load_policy_book

ROOT = os.path.dirname(os.path.dirname(__file__))
STATIC = os.path.join(ROOT, "static")

app = FastAPI(title="Weather Advisory Support Bot")


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    citations: list[str]
    thread_id: str
    severity: str | None = None
    policy_id: str | None = None
    location: str | None = None
    activity: str | None = None
    trace: list[str] = []


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    thread_id = req.thread_id or str(uuid.uuid4())
    out = ask(req.message, thread_id=thread_id)
    return ChatResponse(
        reply=out["answer"],
        citations=out["citations"],
        thread_id=thread_id,
        severity=out.get("severity"),
        policy_id=out.get("policy_id"),
        location=out.get("location"),
        activity=out.get("activity"),
        trace=out.get("trace", []),
    )


@app.get("/policies")
def policies():
    """Lets a reviewer see the live rule set without reading the file."""
    book = load_policy_book()
    return {
        "count": len(book.sops),
        "conflict_resolution": book.config["conflict_resolution"],
        "sops": [
            {
                "id": s.id,
                "category": s.category,
                "severity": s.severity,
                "title": s.title,
                "fuzzy": s.fuzzy,
                "source": s.source,
            }
            for s in book.sops
        ],
    }


@app.get("/")
def home():
    """Landing page."""
    return FileResponse(os.path.join(STATIC, "home.html"))


@app.get("/app")
def chat_ui():
    """The chat interface."""
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")