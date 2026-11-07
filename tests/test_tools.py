"""Tools, filters, the agent loop and the HTTP API on a three-offer catalog with a fake embedder."""
import json
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient

from job_matcher import agent
from job_matcher.api import create_app
from job_matcher.catalog import Catalog, Filters, check
from job_matcher.tools import Toolbox


def card(oid, title, level, years=None, city="Kraków", remote="office", family="ml_ai"):
    ex = {"seniority": level, "seniority_quote": "", "min_years": years, "years_quote": "", "role_family": family}
    return {"id": oid, "title": title, "company": "Acme", "url": f"https://x/{oid}", "locations": [city],
            "remote": remote, "level": level, "min_years": years, "role_family": family, "must_have": ["Python"],
            "nice_to_have": [], "board_level": None, "extraction": ex, "regex_years": None}


CARDS = [card("a", "Junior ML Engineer", "junior"),
         card("b", "Senior ML Engineer", "senior", 5),
         card("c", "Data Engineer", "mid", 4, city="Gdańsk", remote="remote", family="data_engineering")]
VECS = np.eye(3, dtype=np.float32)


class FakeEmbedder:
    name = "fake"

    def embed_query(self, text):
        return [1.0, 0.0, 0.0] if "ml" in text.lower() else [0.0, 0.0, 1.0]


def toolbox():
    return Toolbox(Catalog(CARDS, VECS, {"source": "test", "created": "2026-11-07", "offers": 3}), FakeEmbedder())


def test_filters_give_reasons():
    assert check(CARDS[0], Filters()) is None
    assert check(CARDS[1], Filters()).startswith("level senior")
    assert check(CARDS[1], Filters(max_seniority="any", max_years=None)) is None
    assert check(CARDS[0], Filters(cities=["Wrocław"])).startswith("location")
    assert check(CARDS[2], Filters(max_seniority="any", max_years=None, cities=["Wrocław"])) is None  # remote
    assert check(CARDS[0], Filters(role_families=["mobile"])).startswith("role")


def test_search_counts_what_the_filters_removed():
    r = toolbox().search_offers("ML engineer")
    assert [x["id"] for x in r["results"]] == ["a"]
    assert r["passed_filters"] == 1 and sum(r["excluded_by"].values()) == 2


def test_call_validates_arguments():
    tb = toolbox()
    assert "error" in json.loads(tb.call("search_offers", '{"query": "x", "max_seniority": "wizard"}'))
    assert "error" in json.loads(tb.call("no_such_tool", "{}"))
    assert json.loads(tb.call("get_offer", '{"offer_id": "a"}'))["title"] == "Junior ML Engineer"
    names = [t["function"]["name"] for t in tb.openai_tools()]
    assert names == ["search_offers", "get_offer", "skill_demand", "catalog_info"]


def test_agent_loop_runs_tools_until_an_answer():
    calls = []

    def tool_call(name, args):
        fn = SimpleNamespace(name=name, arguments=json.dumps(args))
        return SimpleNamespace(id=f"t{len(calls)}", function=fn,
                               model_dump=lambda **_: {"id": "t", "type": "function",
                                                       "function": {"name": name, "arguments": json.dumps(args)}})

    replies = iter([SimpleNamespace(content="", tool_calls=[tool_call("search_offers", {"query": "ML"})]),
                    SimpleNamespace(content="Junior ML Engineer at Acme.", tool_calls=None)])

    class Model:
        def chat(self, messages, tools):
            calls.append(messages[-1]["role"])
            return next(replies)

    answer = agent.run("ML jobs?", toolbox(), Model(), log=lambda *_: None)
    assert answer == "Junior ML Engineer at Acme."
    assert calls == ["user", "tool"]


def test_api_search_and_mcp():
    with TestClient(create_app(toolbox())) as client:
        r = client.post("/api/search", json={"query": "ML engineer", "max_seniority": "any", "max_years": None})
        assert [x["id"] for x in r.json()["results"]][:2] == ["a", "b"]
        assert client.get("/api/offers/zzz").status_code == 404
        r = client.post("/mcp", headers={"accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert {t["name"] for t in r.json()["result"]["tools"]} >= {"search_offers", "get_offer"}
