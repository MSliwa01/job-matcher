"""Turn a job search log into graded relevance labels for the retrieval evaluation.

Input (the author's own job search, kept outside this repository):
  --apps    folder with one sub-folder per tailored application, each holding ad.md with the offer URL
  --status  Markdown table with one row per application; the CV path names the folder, the 7th column
            holds the status in Polish ("wysłane" = sent, "rozmowa" = interview, ...)
  --jobs    the fetchers' JSONL store, to map URLs to offer ids

Grades:
  3  the application led to an interview, screening call or test task
  2  sent
  1  shortlisted and a CV tailored, but not sent (skipped, postponed, offer closed)
Every other offer counts as 0. That includes offers nobody looked at closely, so the metrics measure
agreement with past decisions, not absolute relevance.

Output: JSONL with offer_id, grade, status. It names companies the author applied to, so it goes to
data/ (git-ignored); only aggregate metrics are published.
"""
import argparse
import json
import re
from pathlib import Path

INTERVIEW = re.compile(r"(?<!bez )rozmow|zaprosz|screening|test|zadani|rembrandt", re.I)
SENT = re.compile(r"wysłane", re.I)


def norm(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apps", type=Path, required=True)
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--jobs", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("data/labels.jsonl"))
    a = p.parse_args()

    by_url = {}
    for line in a.jobs.read_text().splitlines():
        if line.strip():
            j = json.loads(line)
            for u in [j["url"], *j.get("also_at", [])]:
                by_url[norm(u)] = j["id"]

    status_by_dir = {}
    for row in a.status.read_text().splitlines():
        cells = [c.strip() for c in row.split("|")]
        if len(cells) < 9 or not (m := re.search(r"`(\d{4}-\d{2}-\d{2}-[^/`]+)/", cells[5])):
            continue
        status_by_dir[m.group(1)] = cells[7]

    labels, unmatched = {}, []
    for d in sorted(a.apps.glob("2026-*")):
        ad = d / "ad.md"
        if not ad.exists():
            continue
        ids = {by_url[norm(u)] for u in re.findall(r"https?://[^\s)>\]\"'`]+", ad.read_text()) if norm(u) in by_url}
        if not ids:
            unmatched.append(d.name)
            continue
        status = status_by_dir.get(d.name, "")
        grade = 3 if INTERVIEW.search(status) else 2 if SENT.search(status) else 1
        for oid in ids:
            if grade > labels.get(oid, {}).get("grade", 0):
                labels[oid] = {"offer_id": oid, "grade": grade, "status": status[:200], "dir": d.name}

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in labels.values()))
    counts = {g: sum(v["grade"] == g for v in labels.values()) for g in (3, 2, 1)}
    print(f"{len(labels)} labelled offers: interview/test {counts[3]}, sent {counts[2]}, "
          f"shortlisted {counts[1]}; {len(unmatched)} folders without a matching offer")


if __name__ == "__main__":
    main()
