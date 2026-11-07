"""Embedding providers behind one small interface, and the job that fills the embeddings table."""
from __future__ import annotations

import os
from typing import Protocol

MAX_CHARS = 6000  # long ads repeat boilerplate after the first few thousand characters


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """BAAI/bge-m3 through sentence-transformers. Free, multilingual, 1024 dimensions."""

    def __init__(self, model: str = "BAAI/bge-m3", max_tokens: int = 512):
        from sentence_transformers import SentenceTransformer

        self.name = f"local:{model}"
        self.model = SentenceTransformer(model)
        self.model.max_seq_length = max_tokens

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, batch_size=16, normalize_embeddings=True,
                                 show_progress_bar=len(texts) > 64).tolist()


class OpenAIEmbedder:
    def __init__(self, model: str = "text-embedding-3-small"):
        from openai import OpenAI

        self.name = f"openai:{model}"
        self.model = model
        self.client = OpenAI()

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 256):
            resp = self.client.embeddings.create(model=self.model, input=texts[i:i + 256])
            out.extend(d.embedding for d in resp.data)
        return out


def get_embedder(spec: str | None = None) -> Embedder:
    spec = spec or os.environ.get("JM_EMBEDDER", "local")
    if spec == "local":
        return LocalEmbedder()
    if spec == "openai":
        return OpenAIEmbedder()
    raise ValueError(f"unknown embedder {spec!r} (local, openai)")


def offer_text(title: str, skills: list[str], description: str) -> str:
    parts = [title]
    if skills:
        parts.append("Skills: " + ", ".join(skills))
    if description:
        parts.append(description)
    return "\n".join(parts)[:MAX_CHARS]


def embed_offers(conn, embedder: Embedder, only_missing: bool = True) -> int:
    sql = """SELECT o.id, o.title, o.skills_must || o.skills_nice, o.description FROM offers o"""
    if only_missing:
        sql += " WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.offer_id = o.id AND e.model = %s)"
    rows = conn.execute(sql, (embedder.name,) if only_missing else ()).fetchall()
    if not rows:
        return 0
    vectors = embedder.embed([offer_text(t, s, d) for _, t, s, d in rows])
    import numpy as np

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO embeddings (offer_id, model, embedding) VALUES (%s, %s, %s) "
            "ON CONFLICT (offer_id, model) DO UPDATE SET embedding = EXCLUDED.embedding",
            [(r[0], embedder.name, np.asarray(v, dtype=np.float32)) for r, v in zip(rows, vectors)],
        )
    return len(rows)
