"""Scoring evaluation on the judged pool: does the LLM score separate applied offers from rejected ones?

Runs the scorer on every offer in the pool that has no score from it yet, then reports AUC, nDCG and
recall at the top, next to an optional reference ranking (e.g. the earlier judging pipeline's scores,
one 'offer_id<TAB>score' per line).
"""
import argparse
import json
import time
from pathlib import Path

from job_matcher import db
from job_matcher.llm import ChatModel
from job_matcher.score import PROMPT_VERSION, Evidence, score_offers
from retrieval import auc, metrics


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--labels", type=Path, default=Path("data/labels.jsonl"))
    p.add_argument("--pool", type=Path, default=Path("data/pool_judged.txt"))
    p.add_argument("--evidence", type=Path, default=Path("data/evidence.txt"))
    p.add_argument("--coursework", type=Path, default=Path("data/coursework.txt"))
    p.add_argument("--excluded", type=Path, default=Path("data/excluded.txt"))
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--reference", type=Path, help="TSV offer_id, score of another ranker to compare against")
    a = p.parse_args()

    pool = [l.strip() for l in a.pool.read_text().splitlines() if l.strip()]
    grades = {r["offer_id"]: r["grade"] for r in map(json.loads, a.labels.read_text().splitlines())
              if r["offer_id"] in pool}
    conn = db.connect()
    model = ChatModel(a.provider, a.model)
    scorer = f"score-{PROMPT_VERSION}:{model.name}"
    ev = Evidence.from_files(a.evidence, a.coursework, a.excluded)

    t0 = time.time()
    stats = score_offers(conn, model, ev, pool, scorer, workers=a.workers)
    u = model.usage
    print(f"{scorer}: {stats}, {time.time() - t0:.0f}s, {u.calls} calls, "
          f"{u.input_tokens} input + {u.output_tokens} output tokens"
          + (f", API-equivalent ${u.api_equivalent_usd:.2f}" if u.api_equivalent_usd else ""))

    scores = {r[0]: r[1] for r in conn.execute(
        "SELECT offer_id, (data->>'score')::int FROM extractions WHERE extractor = %s AND offer_id = ANY(%s)",
        (scorer, pool))}
    rankers = {scorer: scores}
    if a.reference:
        rankers["reference"] = {o: float(s) for o, s in
                                (l.split("\t") for l in a.reference.read_text().splitlines() if l.strip())}
    print(f"\n{len(pool)} offers in pool, {len(grades)} applied")
    print(f"{'ranker':45}{'scored':>7}{'AUC':>7}{'R@20':>7}{'R@50':>7}{'nDCG@100':>9}")
    for name, sc in rankers.items():
        common = [o for o in pool if o in sc]
        g = {o: v for o, v in grades.items() if o in sc}
        r = sorted(common, key=lambda o: -sc[o])
        m = metrics(r, g, len(r))
        print(f"{name:45}{len(common):7}{auc(r, g):7.3f}{m['R@20']:7.3f}{m['R@50']:7.3f}{m['nDCG@100']:9.3f}")


if __name__ == "__main__":
    main()
