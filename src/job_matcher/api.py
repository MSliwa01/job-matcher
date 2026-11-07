"""HTTP API and demo page over a snapshot of the offers, plus the MCP server at /mcp.

No language model runs here: search is the int8 ONNX embedding of the query on the CPU and a dot product
against the stored vectors, then the gate. The same container image runs on Azure Container Apps and on
AWS Lambda (through the Lambda Web Adapter).

    JM_SNAPSHOT=snapshot uv run uvicorn --factory job_matcher.api:create_app --port 8080
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from job_matcher import mcp_server
from job_matcher.tools import Family, Level, Toolbox, toolbox_from_env

PER_IP = int(os.environ.get("JM_RATE_PER_IP", "30"))       # requests a minute from one address
GLOBAL = int(os.environ.get("JM_RATE_GLOBAL", "300"))      # requests a minute in total, a cap on the bill


class Limiter:
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


class SearchBody(BaseModel):
    query: str = Field(min_length=2, max_length=20000, description="Keywords, a role, or a whole CV")
    max_seniority: Level = "junior"
    max_years: int | None = 2
    cities: list[str] = []
    remote_only: bool = False
    role_families: list[Family] = []
    limit: int = Field(10, ge=1, le=50)


WEB = Path(__file__).parent / "web"


def create_app(toolbox: Toolbox | None = None) -> FastAPI:
    toolbox = toolbox or toolbox_from_env()
    mcp = mcp_server.build(toolbox)
    mcp_app = mcp.streamable_http_app(json_response=True, stateless_http=True, host="0.0.0.0")

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="job-matcher", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.router.routes.extend(mcp_app.routes)
    limiter = Limiter()

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        if request.url.path.startswith(("/api/search", "/api/skills", "/mcp")):
            ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")
            ip = ip.split(",")[0].strip()
            if not limiter.allow("*", GLOBAL) or not limiter.allow(ip, PER_IP):
                return JSONResponse({"error": "too many requests, try again in a minute"}, status_code=429)
        return await call_next(request)

    @app.post("/api/search")
    def search(body: SearchBody) -> dict:
        return toolbox.search_offers(**body.model_dump())

    @app.get("/api/search")
    def search_get(q: str = Query(min_length=2, max_length=2000), max_seniority: Level = "junior",
                   max_years: int | None = 2, city: list[str] = Query([]), remote_only: bool = False,
                   role: list[Family] = Query([]), limit: int = Query(10, ge=1, le=50)) -> dict:
        return toolbox.search_offers(q, max_seniority, max_years, city, remote_only, role, limit)

    @app.get("/api/offers/{offer_id:path}")
    def offer(offer_id: str) -> dict:
        out = toolbox.get_offer(offer_id)
        if "error" in out:
            raise HTTPException(404, out["error"])
        return out

    @app.get("/api/skills")
    def skills(max_seniority: Level = "junior", city: list[str] = Query([]), role: list[Family] = Query([]),
               top: int = Query(20, ge=5, le=50)) -> dict:
        return toolbox.skill_demand(max_seniority, city, role, top)

    @app.get("/api/info")
    def info() -> dict:
        return toolbox.catalog_info()

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "offers": len(toolbox.catalog.cards)}


    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    return app
