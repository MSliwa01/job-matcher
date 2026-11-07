"""Full-text, vector and hybrid (reciprocal rank fusion) search over offers."""
from __future__ import annotations

import numpy as np

RRF_K = 60  # standard constant from Cormack et al. (2009); damps the weight of top ranks


# ts_rank_cd normalisation: 1 divides by 1 + log(document length), so long ads stop winning on volume.
FT_NORM = 1


def fulltext(conn, query: str, limit: int = 50, open_only: bool = True, norm: int = FT_NORM) -> list[str]:
    """OR over the query's terms, ranked by ts_rank_cd. Works for a short question and for a whole CV,
    where an AND query would match nothing."""
    terms = conn.execute(
        "SELECT array_agg(DISTINCT lexeme) FROM unnest(to_tsvector('simple', %s)) WHERE length(lexeme) > 2",
        (query,),
    ).fetchone()[0] or []
    if not terms:
        return []
    tsq = " | ".join("'" + t.replace("'", "''") + "'" for t in terms)
    rows = conn.execute(
        f"""SELECT id FROM offers, to_tsquery('simple', %s) q
            WHERE tsv @@ q {"AND NOT closed" if open_only else ""}
            ORDER BY ts_rank_cd(tsv, q, %s) DESC LIMIT %s""",
        (tsq, norm, limit),
    ).fetchall()
    return [r[0] for r in rows]


def vector(conn, embedder, query: str, limit: int = 50, open_only: bool = True) -> list[str]:
    q = np.asarray(embedder.embed_query(query), dtype=np.float32)
    rows = conn.execute(
        f"""SELECT e.offer_id FROM embeddings e JOIN offers o ON o.id = e.offer_id
            WHERE e.model = %s {"AND NOT o.closed" if open_only else ""}
            ORDER BY e.embedding <=> %s LIMIT %s""",
        (embedder.name, q, limit),
    ).fetchall()
    return [r[0] for r in rows]


def rrf(*rankings: list[str], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, oid in enumerate(ranking):
            scores[oid] = scores.get(oid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=scores.__getitem__, reverse=True)


def hybrid(conn, embedder, query: str, limit: int = 20, depth: int = 100, open_only: bool = True) -> list[str]:
    return rrf(fulltext(conn, query, depth, open_only), vector(conn, embedder, query, depth, open_only))[:limit]
