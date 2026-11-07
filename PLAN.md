# Plan

## Pipeline

A funnel, so the expensive model only sees offers that survived the cheap steps:

1. **Retrieve** (no LLM, free): hybrid search over all offers. Full text and vectors merged with
   reciprocal rank fusion, the CV (or a question) as the query.
2. **Gate** (deterministic): hard constraints as pass/fail with a reason: location or remote,
   seniority, required language, minimum salary. An offer that fails is not "a 40% match"; it is
   listed separately with the reason, never dropped silently.
3. **Score** (LLM, top N only): each requirement from the ad rated on an anchored 0-4 scale, and
   every rating quotes its evidence: a line from the ad and a line from the CV or evidence file.
   No quote, no point.
4. **Trace**: every stage writes what it decided and why, so a ranking can be explained afterwards.

Each run ends with a cost line (tokens, calls, dollars; zero in local mode).

## Stages

1. Postgres + pgvector, import offers from the fetchers' JSONL, full-text search. Done.
2. Embeddings behind one interface: local `bge-m3` (free, good with Polish) and OpenAI
   `text-embedding-3-small`. Hybrid search with reciprocal rank fusion.
3. LLM extraction with structured output (stack, seniority, salary, work mode, language), feeding the gate.
   Providers: OpenAI API, Gemini, and a local mode through a subscription CLI (`claude -p` / `codex exec`).
4. Scoring with evidence quotes, then an agent with tools (`search_offers`, `get_offer`, `score_offer`)
   that answers with offer ids. Also exposed as an MCP server, so a coding agent can query the database.
5. Evaluation on real decisions: offers applied to vs. offers seen and skipped, and offers that led to an
   interview. Retrieval: recall@k and nDCG for full text only, vectors only, hybrid, hybrid + reranking.
   Extraction: accuracy on hand-checked ads. Scoring: agreement with the decisions, and the spread of
   scores between providers on the same offer.
6. FastAPI + small web UI, Docker image, deployed to Azure Container Apps and AWS App Runner with
   Terraform and GitHub Actions. Hard spending cap on the API key.

## Not doing

Automatic applying, cover letters, recruiter outreach. Other projects do those; this one is about
finding and ranking offers and measuring whether the ranking is right.

Ad texts stay out of the repository: it holds code, evaluation results and links only.
