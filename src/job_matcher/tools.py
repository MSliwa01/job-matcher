"""The tools an agent can call, defined once and served three ways: to the agent loop in agent.py
(OpenAI-style function calling), by the MCP server in mcp_server.py, and by the HTTP API in api.py.

Each tool is a plain function with typed, described parameters. `schema()` turns the signature into the
JSON schema that function calling needs; the MCP SDK reads the same signature on its own.
"""
from __future__ import annotations

import inspect
import json
import os
from functools import lru_cache
from typing import Annotated, Literal, get_type_hints

from pydantic import Field, create_model

from job_matcher.catalog import Catalog, Filters

Level = Literal["intern", "junior", "mid", "senior", "lead", "any"]
Family = Literal["backend", "frontend", "fullstack", "mobile", "data_engineering", "data_analysis", "ml_ai",
                 "qa_testing", "devops_sre", "it_support_ops", "security", "embedded", "product_design",
                 "sales_marketing", "other_business"]


class Toolbox:
    def __init__(self, catalog: Catalog, embedder, cache_size: int = 512):
        self.catalog = catalog
        self.embedder = embedder
        self._embed = lru_cache(cache_size)(lambda text: tuple(embedder.embed_query(text)))

    def search_offers(
        self,
        query: Annotated[str, Field(description="What to look for: a role, skills, or a whole CV as plain text")],
        max_seniority: Annotated[Level, Field(description="Highest level to keep; 'any' keeps all")] = "junior",
        max_years: Annotated[int | None, Field(description="Drop offers asking for more years; null = no limit")] = 2,
        cities: Annotated[list[str], Field(description="Acceptable cities; remote offers always pass")] = [],
        remote_only: Annotated[bool, Field(description="Keep only fully remote offers")] = False,
        role_families: Annotated[list[Family], Field(description="Keep only these role families; empty = any")] = [],
        limit: Annotated[int, Field(ge=1, le=50, description="How many offers to return")] = 10,
    ) -> dict:
        """Semantic search over open job offers in Poland, nearest first, after hard filters on level, years
        of experience, location and role family. Also says how many offers each filter removed."""
        f = Filters(max_seniority, max_years, list(cities), remote_only, list(role_families))
        return self.catalog.search(list(self._embed(query[:20000])), f, limit)

    def get_offer(
        self,
        offer_id: Annotated[str, Field(description="An id returned by search_offers")],
    ) -> dict:
        """Everything known about one offer: link, location, salary, the level and years the ad asks for with
        the quotes that show it, required and optional skills. Includes the ad text when running locally."""
        return self.catalog.get(offer_id) or {"error": f"no open offer with id {offer_id!r}"}

    def skill_demand(
        self,
        max_seniority: Annotated[Level, Field(description="Highest level to count; 'any' counts all")] = "junior",
        cities: Annotated[list[str], Field(description="Acceptable cities; remote offers always count")] = [],
        role_families: Annotated[list[Family], Field(description="Count only these role families; empty = any")] = [],
        top: Annotated[int, Field(ge=5, le=50)] = 20,
    ) -> dict:
        """Which skills open offers require most often, counted over offers that pass the filters."""
        f = Filters(max_seniority, None, list(cities), False, list(role_families))
        return self.catalog.skill_counts(f, top)

    def catalog_info(self) -> dict:
        """When the data was collected, how many open offers there are, and their split by level, role
        family and work mode."""
        return self.catalog.stats()

    def functions(self) -> dict:
        return {n: getattr(self, n) for n in ("search_offers", "get_offer", "skill_demand", "catalog_info")}

    def call(self, name: str, arguments: str | dict) -> str:
        fn = self.functions().get(name)
        if fn is None:
            return json.dumps({"error": f"unknown tool {name!r}"})
        args = json.loads(arguments or "{}") if isinstance(arguments, str) else arguments
        try:
            model = _args_model(fn)
            clean = model.model_validate(args).model_dump()
            return json.dumps(fn(**clean), ensure_ascii=False, default=str)
        except Exception as e:  # the model sees the error and can retry with other arguments
            return json.dumps({"error": f"{type(e).__name__}: {e}"[:500]})

    def openai_tools(self) -> list[dict]:
        return [{"type": "function", "function": {"name": n, "description": inspect.getdoc(fn),
                                                  "parameters": schema(fn)}}
                for n, fn in self.functions().items()]


def _args_model(fn):
    hints = get_type_hints(fn, include_extras=True)
    fields = {}
    for name, p in inspect.signature(fn).parameters.items():
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[name] = (hints[name], default)
    return create_model(f"{fn.__name__}_args", **fields)


def schema(fn) -> dict:
    s = _args_model(fn).model_json_schema()
    s.pop("title", None)
    for prop in s.get("properties", {}).values():
        prop.pop("title", None)
    return s


def toolbox_from_env() -> Toolbox:
    """JM_SNAPSHOT=<dir> serves a snapshot; otherwise the live database. Queries are embedded with the
    int8 ONNX model on the CPU unless JM_QUERY_EMBEDDER says otherwise."""
    from job_matcher.embed import get_embedder

    emb = get_embedder(os.environ.get("JM_QUERY_EMBEDDER", "onnx"))
    if snap := os.environ.get("JM_SNAPSHOT"):
        return Toolbox(Catalog.from_dir(snap), emb)
    from job_matcher import db

    conn = db.connect()
    return Toolbox(Catalog.from_db(conn, os.environ.get("JM_EXTRACTOR", "claude-cli:haiku"), emb.name,
                                   scorer=os.environ.get("JM_FIT_SCORER", "score-v2:claude-cli:haiku")), emb)
