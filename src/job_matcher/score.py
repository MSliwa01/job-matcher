"""Score an offer against the candidate's evidence. The model judges each requirement and quotes the
evidence; code checks the quotes and computes the score, so a confident sentence without a source
cannot raise it.

Evidence comes in three plain-text files:
  evidence    what the candidate has built or done; a quote from here can make a requirement "met"
  coursework  courses and classes; a quote from here counts as "partial" at most
  excluded    things the candidate does not want claimed; a quote from here counts as "missing"

The score formula follows the author's earlier judging script: 70 points for must-haves, 15 for
nice-to-haves, 10 for level fit, 5 for domain fit; any dealbreaker caps the score at 40.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from job_matcher.extract import ad_text
from job_matcher.llm import ChatModel

STATUS_VALUE = {"met": 1.0, "partial": 0.5, "missing": 0.0}
PROMPT_VERSION = "v2"   # v1: dealbreakers undefined; v2: dealbreakers defined, short ads skipped
MIN_AD_CHARS = 300      # below this an ad has no requirements to judge, and models invent them


class Requirement(BaseModel):
    text: str = Field(description="The requirement in a few words, as the ad states it")
    kind: Literal["must", "nice"]
    weight: int = Field(description="1 = minor, 2 = clearly wanted, 3 = central to the role")
    status: Literal["met", "partial", "missing"]
    evidence: str = Field(description="Verbatim quote, at most 20 words, copied from the candidate's evidence "
                                      "that supports the status; '' when missing")


class Judgment(BaseModel):
    requirements: list[Requirement] = Field(description="Every requirement the ad lists, at most 15")
    seniority_fit: float = Field(description="0-1: does the level the ad hires for match an entry-level candidate")
    domain_fit: float = Field(description="0-1: closeness of the ad's domain to the candidate's work")
    dealbreakers: list[str] = Field(description="Only the kinds listed under DEALBREAKERS in the instructions. "
                                                "Empty if none.")
    summary: str = Field(description="One sentence: the fit and the main gap")


SYSTEM = """You judge how well one candidate fits one job ad, requirement by requirement.
Use only the candidate's evidence below. A requirement is "met" only if the evidence shows it directly,
"partial" if the evidence shows something adjacent, "missing" otherwise. Every "met" or "partial" needs a
verbatim quote from the evidence: copy the words exactly, do not paraphrase. Do not credit skills the
evidence does not name.

DEALBREAKERS are rare. List one only when:
- the ad hires above entry level: mid or senior, or it requires 2+ years of commercial experience;
- the role's main language or stack is one the evidence never shows at all (e.g. a PHP/Laravel role for a
  candidate with no PHP), so the daily work would be in an unknown stack;
- the role's core domain is one the candidate has no contact with (e.g. a security operations centre);
- a legal or human-language requirement the candidate cannot meet.
One missing skill among several, even a required one, is a gap, not a dealbreaker: entry-level hires
are expected to have gaps.

=== CANDIDATE EVIDENCE: work and projects ===
{evidence}

=== CANDIDATE EVIDENCE: coursework (counts as partial at most) ===
{coursework}

=== NOT TO BE CLAIMED ===
{excluded}
"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip("\"'“”„").casefold()


@dataclass
class Evidence:
    evidence: str
    coursework: str = ""
    excluded: str = ""

    @classmethod
    def from_files(cls, evidence: Path, coursework: Path | None = None, excluded: Path | None = None) -> Evidence:
        read = lambda p: p.read_text() if p else ""  # noqa: E731
        return cls(read(evidence), read(coursework), read(excluded))

    def system_prompt(self) -> str:
        return SYSTEM.format(evidence=self.evidence, coursework=self.coursework or "(none)",
                             excluded=self.excluded or "(none)")


def verify(j: dict, ev: Evidence) -> tuple[dict, list[str]]:
    """Downgrade any status whose quote is not really in the evidence."""
    work, course, excl = _norm(ev.evidence), _norm(ev.coursework), _norm(ev.excluded)
    fixes = []
    for r in j["requirements"]:
        q = _norm(r.get("evidence") or "")
        before = r["status"]
        if q and q in excl and q not in work and q not in course:
            r["status"] = "missing"
        elif r["status"] == "met" and not (q and (q in work or q in course)):
            r["status"] = "partial"  # a claim without a verbatim source is worth half at most
        elif r["status"] == "met" and q in course and q not in work:
            r["status"] = "partial"
        elif r["status"] == "partial" and not (q and (q in work or q in course)):
            r["status"] = "missing"
        r["weight"] = min(3, max(1, int(r.get("weight") or 1)))
        if r["status"] != before:
            fixes.append(f"{r['text'][:40]}: {before} -> {r['status']}")
    return j, fixes


def compute_score(j: dict) -> int:
    def share(kind: str) -> float | None:
        reqs = [r for r in j["requirements"] if r["kind"] == kind]
        total = sum(r["weight"] for r in reqs)
        return sum(r["weight"] * STATUS_VALUE[r["status"]] for r in reqs) / total if total else None

    must, nice = share("must"), share("nice")
    if must is None:
        must, nice = nice or 0.0, None
    score = 70 * must + 15 * (nice if nice is not None else must)
    score += 10 * min(1, max(0, j["seniority_fit"])) + 5 * min(1, max(0, j["domain_fit"]))
    if j["dealbreakers"]:
        score = min(score, 40)
    return round(max(0, min(100, score)))


def score_offers(conn, model: ChatModel, ev: Evidence, ids: list[str], scorer: str, workers: int = 6) -> dict:
    """Judge, verify and score; results go to the extractions table under the scorer's name."""
    have = {r[0] for r in conn.execute(
        "SELECT offer_id FROM extractions WHERE extractor = %s AND offer_id = ANY(%s)", (scorer, ids))}
    rows = conn.execute("SELECT id, title, company, description FROM offers WHERE id = ANY(%s) AND length(description) >= %s",
                        ([i for i in ids if i not in have], MIN_AD_CHARS)).fetchall()
    system = ev.system_prompt()
    stats = {"scored": 0, "failed": 0, "downgraded": 0, "requirements": 0}

    def one(row):
        oid, title, company, desc = row
        return oid, model.parse(system, ad_text(title, company, desc), Judgment)

    with ThreadPoolExecutor(workers) as pool:
        for f in as_completed([pool.submit(one, r) for r in rows]):
            try:
                oid, judgment = f.result()
            except Exception as e:
                stats["failed"] += 1
                print("failed:", str(e)[:150])
                continue
            j, fixes = verify(judgment.model_dump(), ev)
            j["score"] = compute_score(j)
            j["fixes"] = fixes
            stats["scored"] += 1
            stats["downgraded"] += len(fixes)
            stats["requirements"] += len(j["requirements"])
            conn.execute("INSERT INTO extractions (offer_id, extractor, data) VALUES (%s, %s, %s) "
                         "ON CONFLICT (offer_id, extractor) DO UPDATE SET data = EXCLUDED.data, created_at = now()",
                         (oid, scorer, Jsonb(j)))
            if stats["scored"] % 20 == 0:
                print(f"  scored {stats['scored']}/{len(rows)}", flush=True)
    return stats
