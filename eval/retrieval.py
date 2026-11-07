"""Retrieval evaluation: how high do the offers the author actually applied to end up?

Every method ranks all offers (open and closed) for one query: a CV, or several CVs fused with RRF.
Metrics over graded labels (eval/build_labels.py):
  R@k     share of labelled offers (grade >= 1) in the top k
  nDCG@k  graded gain 2^grade - 1, so an offer that led to an interview counts most
  MRR     reciprocal rank of the first labelled offer
Random order is the floor: expected R@k = k / N.

Experiments change one thing at a time, so each row shows what that change is worth.
With a gate, offers that fail it go to the end of the ranking (they count as missed in the top k).
"""
import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

from job_matcher import db, gate, search
from job_matcher.embed import get_embedder

KS = (20, 50, 100, 200)
CITIES = ["Zielona Góra", "Gorzów Wielkopolski", "Poznań", "Wrocław", "Kraków", "Gdańsk"]


@dataclass(frozen=True)
class Exp:
    name: str
    method: str  # fulltext | vector | hybrid
    ft_norm: int = 1
    query_tokens: int = 2048
    gate: str | None = None  # None | "seniority" | "seniority+location"
    queries: str = "cv"      # cv | lanes | cv+lanes


EXPERIMENTS = [
    Exp("fulltext, raw rank", "fulltext", ft_norm=32),
    Exp("fulltext, length-normalised", "fulltext", ft_norm=1),
    Exp("vector, CV cut at 512 tokens", "vector", query_tokens=512),
    Exp("vector, whole CV", "vector"),
    Exp("hybrid", "hybrid"),
    Exp("fulltext + gate", "fulltext", gate="seniority"),
    Exp("vector + gate", "vector", gate="seniority"),
    Exp("hybrid + gate", "hybrid", gate="seniority"),
    Exp("hybrid + gate + location", "hybrid", gate="seniority+location"),
    Exp("lanes: fulltext + gate", "fulltext", gate="seniority", queries="lanes"),
    Exp("lanes: vector + gate", "vector", gate="seniority", queries="lanes"),
    Exp("lanes: hybrid + gate", "hybrid", gate="seniority", queries="lanes"),
    Exp("CV+lanes: hybrid + gate", "hybrid", gate="seniority", queries="cv+lanes"),
    Exp("CV+lanes: vector + gate", "vector", gate="seniority", queries="cv+lanes"),
]


def metrics(ranking: list[str], grades: dict[str, int], n_total: int) -> dict:
    pos = {oid: i for i, oid in enumerate(ranking)}
    rel = [oid for oid in grades if grades[oid] > 0]
    out = {f"R@{k}": sum(1 for o in rel if pos.get(o, n_total) < k) / len(rel) for k in KS}
    dcg = sum((2 ** grades[o] - 1) / math.log2(pos[o] + 2) for o in rel if pos.get(o, n_total) < 100)
    ideal = sorted((grades[o] for o in rel), reverse=True)[:100]
    out["nDCG@100"] = dcg / sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    first = min((pos[o] for o in rel if o in pos), default=None)
    out["MRR"] = 1 / (first + 1) if first is not None else 0.0
    return out


def auc(ranking: list[str], grades: dict[str, int]) -> float:
    """Probability that a labelled offer ranks above an unlabelled one."""
    pos_seen, wins, neg = 0, 0, 0
    for oid in ranking:
        if grades.get(oid, 0) > 0:
            pos_seen += 1
        else:
            neg += 1
            wins += len([g for g in grades.values() if g > 0]) - pos_seen  # positives ranked below this negative
    n_pos = sum(1 for g in grades.values() if g > 0)
    return 1 - wins / (n_pos * neg) if neg and n_pos else float("nan")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--labels", type=Path, default=Path("data/labels.jsonl"))
    p.add_argument("--cv", type=Path, nargs="+", required=True, help="one or more CV text files")
    p.add_argument("--lanes", type=Path, help="text file, one target role per line (e.g. 'junior python developer')")
    p.add_argument("--pool", type=Path, help="restrict ranking to these offer ids (one per line): offers the "
                   "author reviewed, so every unlabelled one is a real 'no'")
    p.add_argument("--embedder", default=None)
    p.add_argument("--only", nargs="*", help="run only experiments whose name contains one of these")
    p.add_argument("--json", type=Path, help="also write results here")
    a = p.parse_args()

    grades = {r["offer_id"]: r["grade"] for r in map(json.loads, a.labels.read_text().splitlines())}
    pool = {l.strip() for l in a.pool.read_text().splitlines() if l.strip()} if a.pool else None
    if pool:
        grades = {o: g for o, g in grades.items() if o in pool}
    conn = db.connect()
    n = conn.execute("SELECT count(*) FROM offers").fetchone()[0]
    emb = get_embedder(a.embedder)
    cvs = [c.read_text() for c in a.cv]
    lanes = [l.strip() for l in a.lanes.read_text().splitlines() if l.strip()] if a.lanes else []
    query_sets = {"cv": cvs, "lanes": lanes, "cv+lanes": cvs + lanes}
    cache: dict = {}

    def ranked(method: str, ft_norm: int, query_tokens: int, qset: str) -> list[str]:
        key = (method, ft_norm, query_tokens, qset)
        if key not in cache:
            if hasattr(emb, "query_max_tokens"):
                emb.query_max_tokens = query_tokens
            per_query = []
            for cv in query_sets[qset]:
                ft = search.fulltext(conn, cv, n, open_only=False, norm=ft_norm) if method != "vector" else []
                vec = search.vector(conn, emb, cv, n, open_only=False) if method != "fulltext" else []
                per_query.append(ft if method == "fulltext" else vec if method == "vector" else search.rrf(ft, vec))
            r = search.rrf(*per_query) if len(per_query) > 1 else per_query[0]
            seen = set(r)
            all_ids = [row[0] for row in conn.execute("SELECT id FROM offers ORDER BY id")]
            cache[key] = r + [o for o in all_ids if o not in seen]  # unmatched offers go last
        return cache[key]

    rules = {"seniority": gate.Rules(), "seniority+location": gate.Rules(cities=CITIES)}
    results = {"random": {f"R@{k}": min(1.0, k / (len(pool) if pool else n)) for k in KS} | ({"AUC": 0.5} if pool else {})}
    for e in EXPERIMENTS:
        if a.only and not any(s in e.name for s in a.only) or (e.queries != "cv" and not lanes):
            continue
        r = ranked(e.method, e.ft_norm, e.query_tokens, e.queries)
        if e.gate:
            passed, failed = gate.split(conn, r, rules[e.gate])
            r = passed + [o for o in r if o in failed]
        if pool:
            r = [o for o in r if o in pool]
            results[e.name] = metrics(r, grades, len(r)) | {"AUC": auc(r, grades)}
        else:
            results[e.name] = metrics(r, grades, n)

    cols = [f"R@{k}" for k in KS] + ["nDCG@100", "MRR"] + (["AUC"] if pool else [])
    print(f"{len(pool) if pool else n} offers, {len(grades)} labelled, query: {', '.join(c.name for c in a.cv)}, embedder: {emb.name}")
    print(f"{'experiment':30}" + "".join(f"{c:>9}" for c in cols))
    for name, r in results.items():
        print(f"{name:30}" + "".join(f"{r[c]:9.3f}" if c in r else f"{'-':>9}" for c in cols))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"n": n, "labelled": len(grades), "embedder": emb.name,
                                      "query": [c.name for c in a.cv], "results": results}, indent=2))


if __name__ == "__main__":
    main()
