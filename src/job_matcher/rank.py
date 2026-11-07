"""The ranking the evaluation picked: gate, vector retrieval cut at 300, then the LLM score.

Every step fills only what is missing (embeddings, extractions, scores), so a daily run after a fetch
costs calls for the new offers only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from job_matcher import gate, search
from job_matcher.embed import embed_offers, get_embedder
from job_matcher.extract import extract_offers
from job_matcher.llm import ChatModel
from job_matcher.score import PROMPT_VERSION, Evidence, score_offers


@dataclass
class Ranked:
    id: str
    score: int | None
    title: str
    company: str
    url: str
    where: str
    summary: str
    gaps: list[str]
    dealbreakers: list[str]


def rank(conn, cvs: list[str], ev: Evidence, rules: gate.Rules, model: ChatModel, cut: int = 300,
         since: str | None = None, workers: int = 6, log=print) -> tuple[list[Ranked], dict[str, str]]:
    emb = get_embedder()
    log(f"embeddings: {embed_offers(conn, emb)} new")
    rules.extractor = model.name
    where = "NOT closed" + (" AND first_seen >= %s" if since else "")
    ids = [r[0] for r in conn.execute(f"SELECT id FROM offers WHERE {where}", (since,) if since else ())]
    done, failed = extract_offers(conn, model, ids, workers=workers, progress=False)
    log(f"extractions: {done} new, {failed} failed")

    n = len(ids)
    pool = set(ids)
    ranked = search.rrf(*[[o for o in search.vector(conn, emb, cv, n * 3, open_only=True) if o in pool]
                          for cv in cvs])
    passed, failed_gate = gate.split(conn, ranked, rules)
    head = passed[:cut]
    scorer = f"score-{PROMPT_VERSION}:{model.name}"
    stats = score_offers(conn, model, ev, head, scorer, workers=workers)
    log(f"scores: {stats['scored']} new; {len(passed)} passed the gate, {len(failed_gate)} did not")

    rows = conn.execute(
        """SELECT o.id, o.title, o.company, o.url, o.remote, o.locations, s.data
           FROM offers o LEFT JOIN extractions s ON s.offer_id = o.id AND s.extractor = %s
           WHERE o.id = ANY(%s)""", (scorer, head)).fetchall()
    out = []
    for oid, title, company, url, remote, locs, d in rows:
        d = d or {}
        gaps = [r["text"] for r in d.get("requirements", []) if r["kind"] == "must" and r["status"] == "missing"]
        out.append(Ranked(oid, d.get("score"), title, company, url,
                          ", ".join(filter(None, [remote, ", ".join(locs[:2])])),
                          d.get("summary", "ad too short to score"), gaps, d.get("dealbreakers", [])))
    out.sort(key=lambda r: -(r.score if r.score is not None else -1))
    return out, failed_gate


def to_markdown(items: list[Ranked], limit: int) -> str:
    lines = ["| # | score | role | company | where | why / main gaps |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(items[:limit], 1):
        note = r.summary
        if r.dealbreakers:
            note += " Dealbreaker: " + "; ".join(r.dealbreakers)
        elif r.gaps:
            note += " Gaps: " + ", ".join(r.gaps[:3])
        note = note.replace("|", "/")
        lines.append(f"| {i} | {r.score if r.score is not None else '-'} | [{r.title}]({r.url}) | {r.company} "
                     f"| {r.where} | {note} |")
    return "\n".join(lines) + "\n"


def load_cvs(paths: list[Path]) -> list[str]:
    return [p.read_text() for p in paths]
