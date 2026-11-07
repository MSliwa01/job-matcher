"""Chat models behind one interface, through the OpenAI SDK.

Any OpenAI-compatible endpoint works by changing base_url: OpenAI itself, Azure OpenAI, Gemini's
compatibility endpoint, or a local server (Ollama, vLLM). The default here is Gemini's free tier with
a pool of keys rotated on rate limits, so the whole project runs at no cost.

Configuration (environment or .env):
  JM_LLM            gemini (default) | openai | local
  JM_LLM_MODEL      model name, e.g. gemini-3.5-flash-lite, gpt-5-mini
  GEMINI_API_KEYS   comma-separated keys, or GEMINI_KEYS_FILE pointing at a file that defines them
  OPENAI_API_KEY    for JM_LLM=openai
  JM_LOCAL_URL      for JM_LLM=local, e.g. http://localhost:11434/v1
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import openai
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def load_dotenv(path: Path = Path(".env")) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def gemini_keys() -> list[str]:
    raw = os.environ.get("GEMINI_API_KEYS", "")
    if not raw and (f := os.environ.get("GEMINI_KEYS_FILE")):
        for line in Path(f).read_text().splitlines():
            if "=" in line and "KEYS" in line.split("=", 1)[0]:
                raw = line.split("=", 1)[1]
    return [k.strip().strip('"') for k in raw.split(",") if k.strip()]


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, u) -> None:
        with self.lock:
            self.calls += 1
            if u:
                self.input_tokens += u.prompt_tokens or 0
                self.output_tokens += u.completion_tokens or 0


class ChatModel:
    def __init__(self, provider: str | None = None, model: str | None = None):
        load_dotenv()
        self.provider = provider or os.environ.get("JM_LLM", "gemini")
        if self.provider == "gemini":
            keys = gemini_keys()
            if not keys:
                raise RuntimeError("no Gemini keys: set GEMINI_API_KEYS or GEMINI_KEYS_FILE")
            self.clients = [openai.OpenAI(api_key=k, base_url=GEMINI_URL, max_retries=0, timeout=90) for k in keys]
            self.model = model or os.environ.get("JM_LLM_MODEL", "gemini-3.5-flash-lite")
        elif self.provider == "openai":
            self.clients = [openai.OpenAI(max_retries=0, timeout=90)]
            self.model = model or os.environ.get("JM_LLM_MODEL", "gpt-5-mini")
        elif self.provider == "local":
            url = os.environ.get("JM_LOCAL_URL", "http://localhost:11434/v1")
            self.clients = [openai.OpenAI(api_key="local", base_url=url, max_retries=0, timeout=90)]
            self.model = model or os.environ.get("JM_LLM_MODEL", "qwen3:8b")
        else:
            raise ValueError(f"unknown provider {self.provider!r}")
        # Per-key pacing: each key gets at most `rpm` requests a minute; a key that answers 429 rests a minute.
        self.rpm = int(os.environ.get("JM_RPM_PER_KEY", "8"))
        self._free_at = [0.0] * len(self.clients)
        self._lock = threading.Lock()
        self.usage = Usage()

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    def _acquire(self) -> int:
        with self._lock:
            i = min(range(len(self.clients)), key=self._free_at.__getitem__)
            wait = self._free_at[i] - time.monotonic()
            self._free_at[i] = max(time.monotonic(), self._free_at[i]) + 60 / self.rpm
        if wait > 0:
            time.sleep(wait)
        return i

    def _rest(self, i: int, seconds: float = 60) -> None:
        with self._lock:
            self._free_at[i] = max(self._free_at[i], time.monotonic() + seconds)

    def parse(self, system: str, user: str, schema: type[T], attempts: int = 8) -> T:
        """Structured output: the reply is validated against a Pydantic model."""
        last: Exception | None = None
        for _ in range(attempts):
            i = self._acquire()
            try:
                resp = self.clients[i].chat.completions.parse(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    response_format=schema,
                    temperature=0,
                )
                self.usage.add(resp.usage)
                msg = resp.choices[0].message
                if msg.parsed is None:
                    raise ValueError(f"no parsed output (refusal={msg.refusal!r})")
                return msg.parsed
            except (openai.RateLimitError, openai.PermissionDeniedError) as e:
                last = e
                self._rest(i)
            except (openai.APIConnectionError, openai.InternalServerError, openai.APITimeoutError) as e:
                last = e
                self._rest(i, 10)
            except (ValueError, openai.LengthFinishReasonError) as e:
                last = e
        raise RuntimeError(f"{self.name} failed after {attempts} attempts: {last}")
