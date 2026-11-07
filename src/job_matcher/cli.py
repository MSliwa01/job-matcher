import argparse
from pathlib import Path

from job_matcher import db
from job_matcher.ingest import ingest


def main() -> None:
    p = argparse.ArgumentParser(prog="jm")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create tables")
    pi = sub.add_parser("ingest", help="load offers from a JSONL file")
    pi.add_argument("path", type=Path)
    sub.add_parser("stats", help="row counts")
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
