"""Compare the regex gate with the LLM-extraction gate.

For each gate: how many offers it removes, and how many of the author's applications it wrongly removes.
Then a sample of offers where the two disagree, for checking by hand.
"""
import argparse
import json
import random
from pathlib import Path

from job_matcher import db, gate


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--labels", type=Path, default=Path("data/labels.jsonl"))
    p.add_argument("--extractor", default="claude-cli:haiku")
    p.add_argument("--sample", type=int, default=25)
    a = p.parse_args()

    labelled = {json.loads(l)["offer_id"] for l in a.labels.read_text().splitlines()}
    conn = db.connect()
    ids = [r[0] for r in conn.execute(
        "SELECT o.id FROM offers o JOIN extractions x ON x.offer_id = o.id AND x.extractor = %s", (a.extractor,))]
    _, regex_fail = gate.split(conn, ids, gate.Rules())
    _, llm_fail = gate.split(conn, ids, gate.Rules(extractor=a.extractor))

    print(f"{len(ids)} offers with an extraction, {len(labelled & set(ids))} of them applied to")
    for name, failed in (("regex gate", regex_fail), ("LLM gate", llm_fail)):
        wrong = [o for o in failed if o in labelled]
        print(f"{name:10}: removes {len(failed):4}, of which applied-to {len(wrong)}")
        titles = dict(conn.execute("SELECT id, title FROM offers WHERE id = ANY(%s)", (wrong,)).fetchall())
        for o in wrong:
            print(f"    {titles[o][:60]:60} {failed[o][:90]}")

    only_llm = [o for o in llm_fail if o not in regex_fail]
    only_regex = [o for o in regex_fail if o not in llm_fail]
    print(f"\nremoved only by LLM: {len(only_llm)}, only by regex: {len(only_regex)}")
    rows = dict(conn.execute("SELECT id, title FROM offers WHERE id = ANY(%s)", (only_llm + only_regex,)).fetchall())
    random.seed(7)
    for label, group, reasons in (("only LLM", only_llm, llm_fail), ("only regex", only_regex, regex_fail)):
        print(f"\n--- sample: {label}")
        for o in random.sample(group, min(a.sample, len(group))):
            print(f"  {rows[o][:55]:55} | {reasons[o][:95]}")


if __name__ == "__main__":
    main()
