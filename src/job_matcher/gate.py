"""Hard constraints as pass/fail with a reason. An offer that fails is reported with the reason,
never silently dropped and never turned into a low score."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

TOO_SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leader|head of|director|manager|architect|vp|chief"
    r"|starszy|kierownik|dyrektor|ekspert|expert)\b", re.I)


# "3+ years of professional experience", "2-4 years experience", "minimum 3 lata doświadczenia",
# "doświadczenie min. 3 lata". Company age ("60 years of expertise") does not match: it needs
# "experience"/"doświadczenie" right next to the number.
_N = r"(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s*\+?"
YEARS = [
    re.compile(_N + r"\s*(?:years?|yrs)\.?(?:\s+of)?(?:\s+[\w/-]+){0,3}?\s+(?:experience|exp\b)", re.I),
    re.compile(_N + r"\s*(?:lat[a]?|rok|roku)(?:\s+[\w/-]+){0,2}?\s+do[sś]wiadczen", re.I),
    re.compile(r"do[sś]wiadczen\w*(?:\s+[\w/-]+){0,4}?\s+(?:min\.?|minimum|co najmniej|od)?\s*" + _N
               + r"\s*(?:lat[a]?|rok|roku)", re.I),
]


def fold(text: str) -> str:
    text = text.lower().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).strip()


def required_years(description: str) -> int | None:
    """Smallest experience requirement stated in the ad; None if it states none."""
    found = [int(m.group(1)) for rx in YEARS for m in rx.finditer(description or "")]
    found = [n for n in found if 0 < n <= 15]
    return min(found) if found else None


@dataclass
class Rules:
    max_seniority: str = "junior"  # intern < junior < mid < senior < lead
    max_years: int = 2             # an ad asking for more years of experience than this fails
    cities: list[str] = field(default_factory=list)  # empty = any city
    remote_ok: bool = True


ORDER = ["intern", "junior", "mid", "senior", "lead"]


def check(offer: dict, rules: Rules) -> str | None:
    """Return None if the offer passes, else the reason it fails."""
    level = offer.get("seniority")
    if level in ORDER and ORDER.index(level) > ORDER.index(rules.max_seniority):
        return f"seniority: {level}"
    if level is None and (m := TOO_SENIOR_TITLE.search(offer["title"])):
        return f"title says {m.group(0).lower()!r}"
    if level not in ("intern", "junior") and (years := required_years(offer.get("description", ""))) \
            and years > rules.max_years:
        return f"asks for {years}+ years of experience"
    if rules.cities:
        if rules.remote_ok and offer.get("remote") == "remote":
            return None
        wanted = {fold(c) for c in rules.cities}
        if not any(fold(loc) in wanted for loc in offer.get("locations") or []):
            return f"location: {', '.join(offer.get('locations') or ['unknown'])} ({offer.get('remote') or '?'})"
    return None


def split(conn, ranking: list[str], rules: Rules) -> tuple[list[str], dict[str, str]]:
    """Keep the ranking order; return passing ids and a reason for each failing id."""
    rows = conn.execute(
        "SELECT id, title, seniority, remote, locations, description FROM offers WHERE id = ANY(%s)",
        (ranking,)).fetchall()
    offers = {r[0]: {"title": r[1], "seniority": r[2], "remote": r[3], "locations": r[4], "description": r[5]}
              for r in rows}
    passed, failed = [], {}
    for oid in ranking:
        reason = check(offers[oid], rules)
        if reason:
            failed[oid] = reason
        else:
            passed.append(oid)
    return passed, failed
