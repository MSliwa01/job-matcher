"""The whole funnel over all offers: retrieval + gate, then the scorer on the top N, fused with RRF.

Compared with retrieval + gate alone at the same cut-offs. Offers below the top N keep their
retrieval order, so the scorer can only reorder what retrieval let through.
"""
import argparse
import json
import time
from pathlib import Path

from job_matcher import db, gate, search
from job_matcher.embed import get_embedder
from job_matcher.llm import ChatModel
from job_matcher.score import PROMPT_VERSION, Evidence, score_offers
from retrieval import KS, metrics


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--labels", type=Path, default=Path("data/labels.jsonl"))
    p.add_argument("--cv", type=Path, nargs="+", default=[Path("data/cv_DataAI.txt"), Path("data/cv_Mobile.txt")])
    p.add_argument("--extractor", default="claude-cli:haiku")
    p.add_argument("--top", type=int, nargs="+", default=[50, 100, 150])
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args()

    grades = {r["offer_id"]: r["grade"] for r in map(json.loads, a.labels.read_text().splitlines())}
    conn = db.connect()
    n = conn.execute("SELECT count(*) FROM offers").fetchone()[0]
    emb = get_embedder()
    ranked = search.rrf(*[search.vector(conn, emb, c.read_text(), n, open_only=False) for c in a.cv])
    passed, failed = gate.split(conn, ranked, gate.Rules(extractor=a.extractor))
    base = passed + [o for o in ranked if o in failed]

    model = ChatModel(a.provider, a.model)
    scorer = f"score-{PROMPT_VERSION}:{model.name}"
    ev = Evidence.from_files(Path("data/evidence.txt"), Path("data/coursework.txt"), Path("data/excluded.txt"))
    t0 = time.time()
    stats = score_offers(conn, model, ev, passed[:max(a.top)], scorer, workers=a.workers)
    print(f"scored {stats['scored']} new offers in {time.time() - t0:.0f}s, "
          f"API-equivalent ${model.usage.api_equivalent_usd:.2f}")
    scores = {r[0]: r[1] for r in conn.execute(
        "SELECT offer_id, (data->>'score')::int FROM extractions WHERE extractor = %s", (scorer,))}

    rows = {"retrieval + gate": metrics(base, grades, n)}
    for top in a.top:
        head = passed[:top]
        by_score = sorted(head, key=lambda o: -scores.get(o, -1))
        rows[f"score only, top {top}"] = metrics(by_score + [o for o in base if o not in set(head)], grades, n)
        fused = search.rrf(head, by_score)
        rows[f"RRF(retrieval, score), top {top}"] = metrics(fused + [o for o in base if o not in set(head)], grades, n)

    cols = [f"R@{k}" for k in KS] + ["nDCG@100"]
    print(f"\n{n} offers, {len(grades)} labelled, {len(passed)} pass the gate")
    print(f"{'ranking':36}" + "".join(f"{c:>9}" for c in cols))
    for name, r in rows.items():
        print(f"{name:36}" + "".join(f"{r[c]:9.3f}" for c in cols))


if __name__ == "__main__":
    main()
