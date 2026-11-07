# Plan

1. Postgres + pgvector, import offers from the fetchers' JSONL, full-text search. Done.
2. Embeddings behind one interface: local `bge-m3` (free, good with Polish) and OpenAI
   `text-embedding-3-small`. Hybrid search: vectors and full text merged with reciprocal rank fusion.
3. LLM extraction with structured output (stack, seniority, salary, work mode, language).
   Providers: OpenAI API, Gemini, and a local CLI mode (`claude -p` / `codex exec`) for personal use.
4. Agent with tools (`search_offers`, `get_offer`, `compare_with_cv`) that answers with offer ids.
5. Evaluation on real decisions: applied offers vs. skipped ones (recall@10, nDCG) for vectors only,
   hybrid, and hybrid + reranking; extraction accuracy on hand-checked ads; API vs. free mode.
6. FastAPI + small web UI, Docker image, deployed to Azure Container Apps and AWS App Runner with
   Terraform and GitHub Actions. Hard spending cap on the API key.

Ad texts stay out of the repository: it holds code, evaluation results and links only.
