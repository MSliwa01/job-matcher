# job-matcher

Ranks job offers against one candidate and measures whether the ranking is right. It runs on offers from
Polish job boards and company career pages, with Postgres and pgvector for storage and search, and an LLM
for reading ads and judging fit. An agent, an MCP server and an HTTP API expose the same search tools, and
Terraform deploys the API to Azure and AWS.

Most tools in this space give each offer a score. This one also checks the scores against the
candidate's real decisions: 96 offers from one job search, graded by how far each got (shortlisted,
applied, or further in the process).

## Results

Over 1,532 offers, with the applications as labels:

| ranking | applied offers in the top 100 | nDCG@100 | model calls |
|---|---|---|---|
| vector search with the CV as the query, plus the gate | 32% | 0.25 | 0 |
| the same, then Claude Haiku scores the top 300 | **56%** | **0.49** | 300 |
| Haiku scores every offer that passes the gate | 48% | 0.45 | 878 |

Scoring every offer is worse than scoring the top 300. Offers outside the candidate's direction can
still score well on generic requirements such as Python, Git or English, and the retrieval cut keeps
them out. The full write-up, with the experiments that did not work, is in
[`eval/RESULTS.md`](eval/RESULTS.md). Some findings:

- Full-text search with a whole CV as the query ranked no better than random order.
- Hybrid search (full text and vectors merged with reciprocal rank fusion) scored below vectors alone.
- The gate improved recall more than any model change. 90 of the 96 applications were junior roles, while
  825 fetched offers named no level at all.
- Gemini 3.5 Flash Lite extracted fields well but invented 30% of its evidence quotes when judging fit.
  Haiku's rate was 7%.

## How it works

1. **Gate.** Hard rules decide pass or fail, and every failed offer gets a reason. The level and the
   required years of experience come from LLM extraction, each with a quote from the ad. A regex fallback
   covers offers with no extraction. A range such as "Junior / Mid" counts at its lowest level.
2. **Retrieve.** The `bge-m3` model embeds every ad locally on the GPU, and one query per CV variant
   ranks them by cosine distance in pgvector. The top 300 move on.
3. **Score.** The model rates each requirement of the ad as met, partial or missing, and quotes the
   candidate's evidence word for word. Code then looks up every quote in the evidence file and downgrades
   a status whose quote is not there. Code also computes the score, so a confident sentence without a
   source cannot raise it.

Each step stores its result in Postgres and only processes offers it has not seen, so a daily run costs
calls for the new offers only.

## Agent, MCP server and API

The same four tools (`search_offers`, `get_offer`, `skill_demand`, `catalog_info`) are defined once in
`tools.py` and served three ways:

- **Agent.** `jm ask "..."` answers a question by calling the tools. With an OpenAI-compatible provider it
  runs a function-calling loop written in `agent.py`. With `claude-cli` it runs Claude Code in headless
  mode with the MCP server as its only tools.
- **MCP server.** `jm mcp` serves the tools over stdio to any MCP client, for example
  `claude mcp add jobs -- uv run --directory /path/to/job-matcher jm mcp`.
- **HTTP API.** `jm serve` runs FastAPI with a search page, `/api/search`, `/api/offers/{id}`,
  `/api/skills`, OpenAPI docs at `/api/docs`, and the MCP server over streamable HTTP at `/mcp`.

```text
$ uv run jm ask "Find junior or intern AI/ML roles in Kraków or remote. Which 3 look best for a Python
  developer with RAG and embeddings projects, and what skills do they ask for most?"
  -> search_offers({"query": "Python developer junior AI ML engineer RAG ...", "cities": ["Kraków"], ...})
  -> skill_demand({"cities": ["Kraków"], "max_seniority": "junior", "role_families": ["ml_ai"], "top": 20})
  -> get_offer({"offer_id": "justjoin:c104632e-..."})
  ...
(18s, API-equivalent $0.005)
```

Search runs the gate after the vector ranking and reports how many offers each filter removed, so the
agent can say "nothing junior in Gdańsk" instead of returning senior roles.

## Deployment

The demo serves a snapshot instead of the database: `jm snapshot` exports the open offers' derived fields
(title, company, link, location, extracted level, years and skills) and their vectors. The snapshot holds
no ad text and no scores against anyone's CV. No language model runs in the deployed service. The query
is embedded with an int8 build of bge-m3 on the CPU, which keeps the ranking of the GPU model
([round 5](eval/RESULTS.md#round-5-int8-query-embeddings-on-the-cpu)).

One container image runs on both clouds:

| | Azure | AWS |
|---|---|---|
| compute | Container Apps, scale to zero, at most 1 replica | Lambda with a function URL, 2 GB, reserved concurrency 2 |
| image | public on GHCR | ECR, two newest images kept |
| HTTP | the container's port 8080 | Lambda Web Adapter extension forwards to the same port |
| cost guard | $2.50 monthly budget: email at 40%, a Logic App stops the app at 100% | $2.50 monthly budget: email at 40%, a function sets concurrency to 0 at 100% |

Budgets in both clouds only send alerts, so each deployment adds a switch that the budget alert triggers
and that stops the service. Cost data arrives a few hours late, so the switch limits a runaway bill
rather than stopping it at the exact amount. The API also limits requests per address and in total.

```bash
./deploy/push.sh ghcr 2026-11-07                      # snapshot, build, push to GHCR
terraform -chdir=deploy/azure apply -var image=ghcr.io/<user>/job-matcher-demo:2026-11-07 \
  -var subscription_id=... -var budget_email=...

terraform -chdir=deploy/aws apply -target=aws_ecr_repository.repo -var budget_email=...
./deploy/push.sh aws 2026-11-07
terraform -chdir=deploy/aws apply -var image_tag=2026-11-07 -var budget_email=...
```

## Models

All chat models sit behind one interface with Pydantic-validated structured output.

| `JM_LLM` | what runs | use |
|---|---|---|
| `claude-cli` (default) | `claude -p --json-schema` on your own Claude subscription | local runs |
| `gemini` | the OpenAI SDK against Gemini's OpenAI-compatible endpoint, with a pool of keys | free tier |
| `openai` | the OpenAI SDK against OpenAI or Azure OpenAI | paid API |
| `local` | any OpenAI-compatible server, such as Ollama or vLLM | offline |

The subscription mode is for personal use on your own machine. A deployed service needs an API key.

Measured cost on the API, for comparison: extraction is about $0.13 per 100 ads and scoring about $0.30 per
100 ads with Haiku.

## Setup

Requirements: Docker, [uv](https://docs.astral.sh/uv/), and for local embeddings an NVIDIA GPU (a GTX 1080 embeds 1,500 ads in under three minutes).

```bash
docker compose up -d                      # Postgres 17 with pgvector on 127.0.0.1:5433
uv sync --extra local                     # local embeddings need torch and sentence-transformers
uv run jm ingest offers.jsonl             # one offer per line: id, source, url, company, title, description, ...
uv run jm embed
uv run jm rank --cv cv.txt --evidence evidence.txt --cities Kraków Wrocław -n 25 --out ranking.md
```

`--evidence` is a plain-text file of what you have built or done. A quote from it can make a
requirement "met". Optional `--coursework` quotes count as "partial" at most, and quotes from
`--excluded` count as "missing".

Other commands: `jm search "junior python fastapi"` for search alone, `jm extract` for extraction alone,
`jm stats` for counts, and `jm ask`, `jm mcp`, `jm serve`, `jm snapshot` from the sections above. The
agent, MCP server and API embed queries with the int8 ONNX model; download `model_int8.onnx` and
`tokenizer.json` from `Xenova/bge-m3` into `~/.cache/jm-models/bge-m3-int8` (or set `JM_ONNX_DIR`).

## Evaluation

```bash
uv run python eval/build_labels.py --apps <applications dir> --status <status table> --jobs offers.jsonl
uv run python eval/retrieval.py --cv cv_a.txt cv_b.txt           # retrieval experiments
uv run python eval/gate_compare.py                               # regex gate vs. LLM gate
uv run python eval/scoring.py --pool judged.txt                  # scorer on a pool of reviewed offers
uv run python eval/funnel.py --top 150 300 500                   # the whole pipeline at several cut-offs
```

The labels name the companies the author applied to, so they stay out of the repository, along with the
ad texts. The repository holds the code and aggregate results.

## Limits

- The labels come from one person's search, and an earlier pipeline shaped which offers were candidates
  at all. The metrics measure agreement with that person's decisions. They are not absolute relevance.
- The scoring prompt v2 was written after looking at the evaluation pool, so its numbers there are
  optimistic. New labelled offers are the real test.
- Full text has no Polish stemmer in Postgres, so it uses the `simple` configuration.
- The demo serves a snapshot. Offers that close after it was taken stay in it until the next push.

## License

MIT
