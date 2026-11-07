import argparse
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
    ps = sub.add_parser("search", help="search open offers")
    ps.add_argument("query", nargs="?", help="question or keywords")
    ps.add_argument("--cv", type=Path, help="use a CV file (text or Markdown) as the query")
    ps.add_argument("--mode", choices=["fulltext", "vector", "hybrid"], default="hybrid")
    ps.add_argument("--embedder", default=None)
    ps.add_argument("-n", type=int, default=10)
    ps.add_argument("--all", action="store_true", help="include offers that look closed")
    a = p.parse_args()

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
