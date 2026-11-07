"""Load offers from a JSONL store (one offer per line) into Postgres.

The expected record shape is the one written by the fetchers in the cv repo (tools/jobs):
id, source, url, company, title, seniority, locations, remote, salary, skills_must,
skills_nice, description, published_at, first_seen, last_seen, possibly_closed.
"""
import json
from pathlib import Path

from psycopg.types.json import Jsonb

UPSERT = """
INSERT INTO offers (id, source, url, company, title, seniority, locations, remote, salary,
                    skills_must, skills_nice, description, published_at, first_seen, last_seen, closed)
VALUES (%(id)s, %(source)s, %(url)s, %(company)s, %(title)s, %(seniority)s, %(locations)s, %(remote)s,
        %(salary)s, %(skills_must)s, %(skills_nice)s, %(description)s, %(published_at)s, %(first_seen)s,
        %(last_seen)s, %(closed)s)
ON CONFLICT (id) DO UPDATE SET
    url = EXCLUDED.url, company = EXCLUDED.company, title = EXCLUDED.title,
    seniority = EXCLUDED.seniority, locations = EXCLUDED.locations, remote = EXCLUDED.remote,
    salary = EXCLUDED.salary, skills_must = EXCLUDED.skills_must, skills_nice = EXCLUDED.skills_nice,
    description = CASE WHEN EXCLUDED.description <> '' THEN EXCLUDED.description ELSE offers.description END,
    published_at = EXCLUDED.published_at, last_seen = EXCLUDED.last_seen, closed = EXCLUDED.closed
"""


def row(r: dict) -> dict:
    return {
        "id": r["id"], "source": r["source"], "url": r["url"], "company": r["company"], "title": r["title"],
        "seniority": r.get("seniority"), "locations": r.get("locations") or [], "remote": r.get("remote"),
        "salary": Jsonb(r.get("salary") or []), "skills_must": r.get("skills_must") or [],
        "skills_nice": r.get("skills_nice") or [], "description": r.get("description") or "",
        "published_at": r.get("published_at"), "first_seen": r.get("first_seen"),
        "last_seen": r.get("last_seen"), "closed": bool(r.get("possibly_closed")),
    }


def ingest(conn, path: Path) -> int:
    rows = [row(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    return len(rows)
