"""Open offers as cards (fields an agent or a page can show) with their vectors, searchable with the gate.

Two sources behind one class:
  Catalog.from_db    live data from Postgres; vector search runs in pgvector
  Catalog.from_dir   a snapshot exported with `jm snapshot`, searched in memory with numpy. The deployed
                     demo uses this, so it needs no database.

A snapshot holds derived fields only: title, company, link, location, and what the LLM extracted
(level, years, skills). It holds no ad text and no scores against anyone's CV.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from job_matcher import gate

LEVELS = ["intern", "junior", "mid", "senior", "lead"]


@dataclass
class Filters:
    max_seniority: str = "junior"          # intern | junior | mid | senior | lead | any
    max_years: int | None = 2              # offers asking for more years fail; None = no limit
    cities: list[str] = field(default_factory=list)   # empty = any city; remote offers always pass
    remote_only: bool = False
    role_families: list[str] = field(default_factory=list)  # empty = any

    def rules(self) -> gate.Rules:
        return gate.Rules(max_seniority="lead" if self.max_seniority == "any" else self.max_seniority,
                          max_years=99 if self.max_years is None else self.max_years, cities=self.cities)


def check(card: dict, f: Filters) -> str | None:
    """None if the card passes the filters, else the reason it does not."""
    if f.remote_only and card.get("remote") != "remote":
        return f"not remote ({card.get('remote') or 'unknown'})"
    if f.role_families and card.get("role_family") not in f.role_families:
        return f"role: {card.get('role_family') or 'unknown'}"
    rules = f.rules()
    offer = {"title": card["title"], "seniority": card.get("board_level"), "remote": card.get("remote"),
             "locations": card.get("locations") or [], "description": "", "extraction": card.get("extraction")}
    reason = gate.check(offer, rules)
    if reason is None and not card.get("extraction") and (y := card.get("regex_years")) and y > rules.max_years:
        reason = f"asks for {y}+ years of experience"
    return reason


def _card(row, extraction: dict | None) -> dict:
    oid, source, url, company, title, level, locs, remote, salary, first_seen, desc = row
    ex = extraction or {}
    return {
        "id": oid, "source": source, "url": url, "company": company, "title": title,
        "locations": list(locs or []), "remote": remote, "salary": salary or [],
        "first_seen": first_seen.date().isoformat() if first_seen else None,
        "board_level": level,
        "level": ex.get("seniority"), "min_years": ex.get("min_years"), "role_family": ex.get("role_family"),
        "must_have": ex.get("must_have", []), "nice_to_have": ex.get("nice_to_have", []),
        "languages": ex.get("languages_required", []), "work_mode": ex.get("work_mode"),
        "extraction": extraction,
        "regex_years": None if extraction else gate.required_years(desc),
    }


class Catalog:
    def __init__(self, cards: list[dict], vectors: np.ndarray | None, meta: dict, conn=None):
        self.cards = {c["id"]: c for c in cards}
        self.order = [c["id"] for c in cards]
        self.vectors = vectors
        self.meta = meta
        self.conn = conn

    # ---- loading and saving

    @classmethod
    def from_db(cls, conn, extractor: str = "claude-cli:haiku", embed_model: str = "local:BAAI/bge-m3",
                scorer: str | None = None) -> Catalog:
        rows = conn.execute(
            """SELECT o.id, o.source, o.url, o.company, o.title, o.seniority, o.locations, o.remote, o.salary,
                      o.first_seen, o.description, x.data, s.data
               FROM offers o
               JOIN embeddings e ON e.offer_id = o.id AND e.model = %s
               LEFT JOIN extractions x ON x.offer_id = o.id AND x.extractor = %s
               LEFT JOIN extractions s ON s.offer_id = o.id AND s.extractor = %s
               WHERE NOT o.closed ORDER BY o.first_seen DESC NULLS LAST, o.id""",
            (embed_model, extractor, scorer)).fetchall()
        cards = []
        for r in rows:
            c = _card(r[:11], r[11])
            c["description"] = r[10]
            if r[12]:
                c["fit"] = {k: r[12].get(k) for k in ("score", "summary", "dealbreakers")}
                c["fit"]["gaps"] = [q["text"] for q in r[12].get("requirements", [])
                                    if q["kind"] == "must" and q["status"] == "missing"]
            cards.append(c)
        meta = {"source": "postgres", "extractor": extractor, "embed_model": embed_model, "offers": len(cards)}
        return cls(cards, None, meta, conn=conn)

    @classmethod
    def from_dir(cls, path: Path | str) -> Catalog:
        path = Path(path)
        meta = json.loads((path / "meta.json").read_text())
        cards = [json.loads(line) for line in (path / "offers.jsonl").read_text().splitlines()]
        vectors = np.load(path / "vectors.npy").astype(np.float32)
        return cls(cards, vectors, meta)

    def save(self, path: Path | str, created: str) -> None:
        """Write a snapshot: cards without ad text or fit, plus the vectors in the same order."""
        if self.conn is None:
            raise ValueError("save works on a catalog loaded from the database")
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        vecs = dict(self.conn.execute(
            "SELECT offer_id, embedding FROM embeddings WHERE model = %s AND offer_id = ANY(%s)",
            (self.meta["embed_model"], self.order)).fetchall())
        keep = [oid for oid in self.order if oid in vecs]
        with (path / "offers.jsonl").open("w") as f:
            for oid in keep:
                c = {k: v for k, v in self.cards[oid].items() if k not in ("description", "fit")}
                if c["extraction"]:  # keep only the fields the gate reads
                    c["extraction"] = {k: c["extraction"].get(k) for k in
                                       ("seniority", "seniority_quote", "min_years", "years_quote", "role_family")}
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        np.save(path / "vectors.npy", np.stack([np.asarray(getattr(vecs[o], "to_numpy", lambda: vecs[o])(), dtype=np.float32) for o in keep]))
        meta = dict(self.meta, source="snapshot", created=created, offers=len(keep))
        (path / "meta.json").write_text(json.dumps(meta, indent=1))

    # ---- queries

    def _ranking(self, qvec: list[float]) -> list[tuple[str, float]]:
        q = np.asarray(qvec, dtype=np.float32)
        if self.vectors is not None:
            sims = self.vectors @ q
            idx = np.argsort(-sims)
            return [(self.order[i], float(sims[i])) for i in idx]
        rows = self.conn.execute(
            """SELECT e.offer_id, 1 - (e.embedding <=> %s) FROM embeddings e JOIN offers o ON o.id = e.offer_id
               WHERE e.model = %s AND NOT o.closed ORDER BY e.embedding <=> %s""",
            (q, self.meta["embed_model"], q)).fetchall()
        return [(oid, float(s)) for oid, s in rows if oid in self.cards]

    def search(self, qvec: list[float], filters: Filters, limit: int = 10) -> dict:
        """Nearest offers that pass the filters. Failed offers are counted with their reasons, not hidden."""
        hits, reasons = [], Counter()
        for oid, sim in self._ranking(qvec):
            reason = check(self.cards[oid], filters)
            if reason:
                reasons[reason.split(":")[0]] += 1
            elif len(hits) < limit:
                hits.append(dict(self.public(oid), similarity=round(sim, 3)))
        excluded = sum(reasons.values())
        return {"results": hits, "searched": len(self.cards), "passed_filters": len(self.cards) - excluded,
                "excluded_by": dict(reasons.most_common(6))}

    def public(self, oid: str) -> dict:
        c = self.cards[oid]
        return {k: c[k] for k in ("id", "title", "company", "url", "locations", "remote", "level", "min_years",
                                   "role_family", "must_have", "first_seen") if k in c} | (
            {"fit_score": c["fit"]["score"]} if c.get("fit") else {})

    def get(self, oid: str, text_chars: int = 3000) -> dict | None:
        c = self.cards.get(oid)
        if c is None:
            return None
        out = {k: v for k, v in c.items() if k not in ("extraction", "regex_years", "description")}
        if c.get("extraction"):
            out["level_quote"] = c["extraction"].get("seniority_quote")
            out["years_quote"] = c["extraction"].get("years_quote")
        if c.get("description"):
            out["ad_text"] = c["description"][:text_chars]
        return out

    def skill_counts(self, filters: Filters, top: int = 20) -> dict:
        """Most-requested must-have skills among open offers that pass the filters."""
        must, nice, n = Counter(), Counter(), 0
        for c in self.cards.values():
            if c.get("extraction") and check(c, filters) is None:
                n += 1
                must.update({s.strip().lower() for s in c["must_have"]})
                nice.update({s.strip().lower() for s in c["nice_to_have"]})
        return {"offers": n, "must_have": must.most_common(top), "nice_to_have": nice.most_common(top // 2)}

    def stats(self) -> dict:
        cs = self.cards.values()
        return dict(self.meta, by_level=Counter(c.get("level") or "not extracted" for c in cs).most_common(),
                    by_role=Counter(c.get("role_family") or "not extracted" for c in cs).most_common(),
                    by_mode=Counter(c.get("remote") or "unknown" for c in cs).most_common())


def filters_from(d: dict) -> Filters:
    known = {k: d[k] for k in asdict(Filters()) if k in d and d[k] is not None}
    return Filters(**known)
