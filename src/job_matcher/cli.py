import argparse
import os
import time
from pathlib import Path

from job_matcher import db, search
from job_matcher.ingest import ingest


def main() -> None:
    p = argparse.ArgumentParser(prog="jm")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create tables")
    pi = sub.add_parser("ingest", help="load offers from a JSONL file")
    pi.add_argument("path", type=Path)
    sub.add_parser("stats", help="row counts")
    pe = sub.add_parser("embed", help="embed offers that have no vector for this model yet")
    pe.add_argument("--embedder", default=None, help="local (default) or openai")
    px = sub.add_parser("extract", help="LLM extraction for offers that have none from this model yet")
    px.add_argument("--provider", default=None, help="gemini (default), openai, local")
    px.add_argument("--model", default=None)
    px.add_argument("--workers", type=int, default=8)
    pr = sub.add_parser("rank", help="gate, retrieval cut, LLM score; prints a short list")
    pr.add_argument("--cv", type=Path, nargs="+", required=True, help="CV text files used as queries")
    pr.add_argument("--evidence", type=Path, required=True, help="what the candidate has done (quotes count as met)")
    pr.add_argument("--coursework", type=Path, help="courses (quotes count as partial at most)")
    pr.add_argument("--excluded", type=Path, help="things not to claim")
    pr.add_argument("--cities", nargs="*", default=[], help="acceptable cities; remote always passes")
    pr.add_argument("--since", help="only offers first seen on or after this date (YYYY-MM-DD)")
    pr.add_argument("--cut", type=int, default=300, help="how many retrieved offers the LLM scores")
    pr.add_argument("-n", type=int, default=25, help="rows to print")
    pr.add_argument("--out", type=Path, help="also write the list as Markdown here")
    pr.add_argument("--provider", default=None)
    pr.add_argument("--model", default=None)
    pr.add_argument("--workers", type=int, default=6)
    ps = sub.add_parser("search", help="search open offers")
    ps.add_argument("query", nargs="?", help="question or keywords")
    ps.add_argument("--cv", type=Path, help="use a CV file (text or Markdown) as the query")
    ps.add_argument("--mode", choices=["fulltext", "vector", "hybrid"], default="hybrid")
    ps.add_argument("--embedder", default=None)
    ps.add_argument("-n", type=int, default=10)
    ps.add_argument("--all", action="store_true", help="include offers that look closed")
    pn = sub.add_parser("snapshot", help="export open offers (derived fields and vectors, no ad text) for the demo")
    pn.add_argument("out", type=Path)
    pn.add_argument("--extractor", default="claude-cli:haiku")
    pq = sub.add_parser("ask", help="ask the agent a question; it answers by calling the search tools")
    pq.add_argument("question")
    pq.add_argument("--provider", default=None, help="claude-cli (default, via MCP), gemini, openai, local")
    pq.add_argument("--model", default=None)
    sub.add_parser("mcp", help="run the MCP server over stdio")
    pv = sub.add_parser("serve", help="run the HTTP API and demo page")
    pv.add_argument("--port", type=int, default=8080)
    a = p.parse_args()

    if a.cmd == "mcp":
        from job_matcher import mcp_server

        return mcp_server.main()
    if a.cmd == "serve":
        import uvicorn

        return uvicorn.run("job_matcher.api:create_app", factory=True, host="127.0.0.1", port=a.port)
    if a.cmd == "ask":
        from job_matcher import agent
        from job_matcher.llm import ChatModel, load_dotenv

        load_dotenv()
        provider = a.provider or os.environ.get("JM_LLM", "claude-cli")
        t0 = time.time()
        if provider == "claude-cli":
            answer, cost = agent.run_claude_cli(a.question, a.model or "haiku")
            note = f"API-equivalent ${cost:.3f}"
        else:
            from job_matcher.tools import toolbox_from_env

            m = ChatModel(provider, a.model)
            answer = agent.run(a.question, toolbox_from_env(), m)
            note = f"{m.usage.calls} model calls, {m.usage.input_tokens} input + {m.usage.output_tokens} output tokens"
        print("\n" + answer + f"\n\n({time.time() - t0:.0f}s, {note})")
        return

    conn = db.connect()
    if a.cmd == "init":
        db.init_schema(conn)
        print("schema ready")
    elif a.cmd == "ingest":
        db.init_schema(conn)
        print(f"upserted {ingest(conn, a.path)} offers")
    elif a.cmd == "stats":
        for t in ("offers", "extractions", "embeddings"):
            print(t, conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
        n = conn.execute("SELECT count(*) FROM offers WHERE description <> ''").fetchone()[0]
        print("offers with full text", n)
        for model, n in conn.execute("SELECT model, count(*) FROM embeddings GROUP BY model"):
            print(f"  {model}: {n}")
    elif a.cmd == "embed":
        from job_matcher.embed import embed_offers, get_embedder

        t0 = time.time()
        emb = get_embedder(a.embedder)
        n = embed_offers(conn, emb)
        print(f"embedded {n} offers with {emb.name} in {time.time() - t0:.0f}s")
    elif a.cmd == "extract":
        from job_matcher.extract import extract_offers
        from job_matcher.llm import ChatModel

        m = ChatModel(a.provider, a.model)
        t0 = time.time()
        done, failed = extract_offers(conn, m, workers=a.workers)
        u = m.usage
        print(f"{m.name}: {done} extracted, {failed} failed, {time.time() - t0:.0f}s, "
              f"{u.calls} calls, {u.input_tokens} input + {u.output_tokens} output tokens")
    elif a.cmd == "rank":
        from job_matcher.gate import Rules
        from job_matcher.llm import ChatModel
        from job_matcher.rank import rank, to_markdown
        from job_matcher.score import Evidence

        m = ChatModel(a.provider, a.model)
        ev = Evidence.from_files(a.evidence, a.coursework, a.excluded)
        items, failed = rank(conn, [c.read_text() for c in a.cv], ev, Rules(cities=a.cities), m,
                             cut=a.cut, since=a.since, workers=a.workers)
        md = to_markdown(items, a.n)
        print(md)
        u = m.usage
        print(f"{u.calls} model calls, {u.input_tokens} input + {u.output_tokens} output tokens"
              + (f", API-equivalent ${u.api_equivalent_usd:.2f}" if u.api_equivalent_usd else ""))
        if a.out:
            a.out.write_text(md)
    elif a.cmd == "snapshot":
        from datetime import date

        from job_matcher.catalog import Catalog

        cat = Catalog.from_db(conn, a.extractor)
        cat.save(a.out, date.today().isoformat())
        print(f"wrote {len(cat.cards)} open offers to {a.out}")
    elif a.cmd == "search":
        query = a.cv.read_text() if a.cv else a.query
        if not query:
            p.error("give a query or --cv")
        open_only = not a.all
        if a.mode == "fulltext":
            ids = search.fulltext(conn, query, a.n, open_only)
        else:
            from job_matcher.embed import get_embedder

            emb = get_embedder(a.embedder)
            ids = (search.vector(conn, emb, query, a.n, open_only) if a.mode == "vector"
                   else search.hybrid(conn, emb, query, a.n, open_only=open_only))
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT id, title, company, remote, locations FROM offers WHERE id = ANY(%s)", (ids,))}
        for i, oid in enumerate(ids, 1):
            title, company, remote, locs = rows[oid]
            where = remote or ", ".join(locs[:2])
            print(f"{i:2}. {title[:60]:60} {company[:28]:28} {where}")
