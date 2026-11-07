"""Embedding providers behind one small interface, and the job that fills the embeddings table."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

MAX_CHARS = 6000  # long ads repeat boilerplate after the first few thousand characters


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class LocalEmbedder:
    """BAAI/bge-m3 through sentence-transformers. Free, multilingual, 1024 dimensions."""

    def __init__(self, model: str = "BAAI/bge-m3", max_tokens: int = 512, query_max_tokens: int = 2048):
        from sentence_transformers import SentenceTransformer

        self.name = f"local:{model}"
        self.model = SentenceTransformer(model)
        self.max_tokens, self.query_max_tokens = max_tokens, query_max_tokens

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.model.max_seq_length = self.max_tokens
        return self.model.encode(texts, batch_size=16, normalize_embeddings=True,
                                 show_progress_bar=len(texts) > 64).tolist()

    def embed_query(self, text: str) -> list[float]:
        """A CV is longer than an ad's useful head, so the query gets a longer window."""
        self.model.max_seq_length = self.query_max_tokens
        return self.model.encode([text], normalize_embeddings=True).tolist()[0]


class OnnxEmbedder:
    """The same bge-m3 as an int8 ONNX model on the CPU, for query embeddings where there is no GPU and
    no room for torch. Ad vectors stay the fp32 ones from LocalEmbedder; eval/onnx_check.py measures how
    far int8 queries move the ranking.

    Files: model_int8.onnx and tokenizer.json from huggingface.co/Xenova/bge-m3, in JM_ONNX_DIR."""

    def __init__(self, path: str | None = None, query_max_tokens: int = 2048, threads: int = 0):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = Path(path or os.environ.get("JM_ONNX_DIR", Path.home() / ".cache/jm-models/bge-m3-int8"))
        self.name = "local:BAAI/bge-m3"  # compatible with the stored fp32 vectors
        self.query_max_tokens = query_max_tokens
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(d / "model_int8.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def _encode(self, text: str, max_tokens: int) -> list[float]:
        import numpy as np

        self.tok.enable_truncation(max_tokens)
        enc = self.tok.encode(text)
        feed = {"input_ids": np.array([enc.ids], dtype=np.int64),
                "attention_mask": np.array([enc.attention_mask], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        hidden = self.session.run(None, feed)[0]
        v = hidden[0, 0]  # bge-m3 dense vectors use the CLS token
        return (v / np.linalg.norm(v)).astype(np.float32).tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._encode(t, 512) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._encode(text, self.query_max_tokens)


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

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def get_embedder(spec: str | None = None) -> Embedder:
    spec = spec or os.environ.get("JM_EMBEDDER", "local")
    if spec == "local":
        return LocalEmbedder()
    if spec == "onnx":
        return OnnxEmbedder()
    if spec == "openai":
        return OpenAIEmbedder()
    raise ValueError(f"unknown embedder {spec!r} (local, onnx, openai)")


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
