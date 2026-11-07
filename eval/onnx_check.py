"""How far int8 ONNX query embeddings (CPU, used in the deployed demo) move the ranking compared with
fp32 sentence-transformers (GPU, used for the measured results). Ad vectors are fp32 in both cases."""
import argparse
import json
import time
from pathlib import Path

import numpy as np

from job_matcher import db, gate, search
from job_matcher.embed import LocalEmbedder, OnnxEmbedder
from retrieval import metrics

QUERIES = ["junior python developer", "machine learning internship Kraków", "data engineer airflow spark",
           "React Native mobile developer", "analityk danych SQL Power BI", "LLM RAG embeddings engineer"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--labels", type=Path, default=Path("data/labels.jsonl"))
    p.add_argument("--cv", type=Path, nargs="+", default=[Path("data/cv_DataAI.txt"), Path("data/cv_Mobile.txt")])
    p.add_argument("--extractor", default="claude-cli:haiku")
    a = p.parse_args()
    conn = db.connect()
    fp32, int8 = LocalEmbedder(), OnnxEmbedder()
    texts = QUERIES + [c.read_text() for c in a.cv]
    t0 = time.time()
    q8 = [int8.embed_query(t) for t in texts]
    print(f"int8 on CPU: {(time.time() - t0) / len(texts):.2f}s per query")
    q32 = [fp32.embed_query(t) for t in texts]
    for t, a8, a32 in zip(texts, q8, q32):
        print(f"cosine fp32 vs int8 {float(np.dot(a8, a32)):.4f}  {t[:40]!r}")

    n = conn.execute("SELECT count(*) FROM offers").fetchone()[0]
    grades = {r["offer_id"]: r["grade"] for r in map(json.loads, a.labels.read_text().splitlines())}

    class Fixed:  # serves precomputed query vectors through the embedder interface
        name = fp32.name

        def __init__(self, vecs):
            self.vecs = vecs

        def embed_query(self, text):
            return self.vecs[text]

    for label, vecs in (("fp32", q32), ("int8", q8)):
        emb = Fixed(dict(zip(texts, vecs)))
        ranked = search.rrf(*[search.vector(conn, emb, c.read_text(), n, open_only=False) for c in a.cv])
        passed, failed = gate.split(conn, ranked, gate.Rules(extractor=a.extractor))
        m = metrics(passed + [o for o in ranked if o in failed], grades, n)
        print(label, " ".join(f"{k}={v:.3f}" for k, v in m.items()))
        if label == "fp32":
            top32 = {t: search.vector(conn, emb, t, 10) for t in QUERIES}
        else:
            overlap = [len(set(top32[t]) & set(search.vector(conn, emb, t, 10))) for t in QUERIES]
            print("top-10 overlap with fp32 per short query:", overlap)


if __name__ == "__main__":
    main()
