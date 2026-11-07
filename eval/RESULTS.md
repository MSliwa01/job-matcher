# Retrieval evaluation, first round (2026-11-07)

## Data

- 1,532 offers from Polish job boards and company career pages (ATS APIs), fetched 2026-10-10 to 2026-10-21.
- Labels from one real job search (`build_labels.py`): 96 offers with a tailored CV, in three grades:
  further in the process (3), sent (2), shortlisted but not sent (1).
- Embeddings: `BAAI/bge-m3`, local, 512 tokens per ad. Query: the two CV variants (data/AI and mobile),
  fused with reciprocal rank fusion.

Labels carry a bias worth stating: offers became candidates through an earlier pipeline (a keyword fetch
filter, then an LLM judging each ad), so an unlabelled offer is often one nobody looked at, not a "no".
Two pools handle that:

- **all offers**: does the method find the applied offers among everything fetched? (recall)
- **judged offers**: 102 offers the author reviewed; 53 applied, 49 rejected. Every unlabelled one is a
  real "no", so AUC is meaningful here. (precision)

## All offers (1,532)

| experiment | R@50 | R@100 | R@200 | nDCG@100 |
|---|---|---|---|---|
| random | 0.033 | 0.065 | 0.131 | |
| full text, CV as query | 0.031 | 0.052 | 0.104 | 0.021 |
| vector, CV cut at 512 tokens | 0.104 | 0.219 | 0.344 | 0.197 |
| vector, whole CV | 0.115 | 0.219 | 0.406 | 0.177 |
| hybrid (RRF) | 0.052 | 0.146 | 0.271 | 0.110 |
| vector + gate | 0.167 | 0.302 | 0.531 | 0.232 |
| hybrid + gate + location | 0.167 | 0.312 | 0.510 | 0.245 |
| role keywords as queries, full text + gate | 0.031 | 0.271 | 0.615 | 0.213 |

## Judged offers (102)

| method | AUC | R@20 | R@50 | nDCG@100 | cost |
|---|---|---|---|---|---|
| random | 0.50 | | | | |
| full text, CV as query | 0.51 | 0.17 | 0.49 | 0.64 | free |
| vector, whole CV | **0.77** | 0.25 | 0.70 | 0.72 | free |
| hybrid (RRF) | 0.71 | 0.25 | 0.66 | 0.68 | free |
| previous pipeline: LLM judges each ad against the CV | **0.86** | 0.38 | 0.77 | 0.83 | one LLM call per ad |

The LLM judge's number is inflated: the author chose from its ranking, so the labels partly echo it.

## What this says

1. **Full text with a CV as the query is no better than random.** A CV shares words with every long ad;
   length normalisation does not fix it. Full text needs short, specific queries.
2. **Hybrid search is not a free win.** Fusing a weak ranker into a good one made it worse in both pools.
   The usual "always do hybrid" advice did not hold on this data, which is the reason to measure.
3. **The gate matters more than the model.** 90 of the 96 applied offers are junior or internship roles,
   while 825 fetched offers state no level at all (mostly big-company ATS feeds, mainly mid and senior).
   Title words plus "N+ years of experience" parsed from the ad remove 628 offers and none of the applied ones.
   Recall@200 went from 0.41 to 0.53.
4. **Embeddings rank well but miss broad searches.** In the judged pool vectors separate yes from no
   (AUC 0.77). Over all offers they still miss half: the author also applied to QA, DevOps and analyst
   roles that the CV text does not describe. Role keywords as queries reach those (R@200 0.62) but scatter
   the top of the list.
5. **An LLM per ad is more precise than any free ranker here**, at a price that grows with every ad.
   So the design is a funnel: free retrieval and the gate pick a few hundred candidates for recall, and the
   LLM scores only those for precision.

## Bugs the evaluation found

- The location gate compared "Krakow" with "Kraków" and dropped four applied offers. Fixed by folding
  diacritics.
- "bez rozmowy" ("without an interview") matched the interview pattern and promoted a rejection to grade 3.

## Round 2: LLM extraction as the gate

Extraction runs through the OpenAI SDK against Gemini's OpenAI-compatible endpoint
(`gemini-3.5-flash-lite`, free tier, structured output validated by Pydantic). Each ad gives its level,
the smallest years requirement, role family, required skills and languages, with a verbatim quote for
the level and the years. 557 of 1,274 ads with text are extracted so far (the free tier allows about six
ads a minute across the key pool); offers without an extraction fall back to the regex gate.

| gate (on 542 extracted offers, 85 applied) | removes | applied offers removed |
|---|---|---|
| regex: title words + "N+ years" | 242 | 0 |
| LLM extraction, first version | 251 | 1 ("Junior / Mid" read as mid) |
| LLM extraction + two rules (range = lowest level; mid asking ≤ 2 years = junior) | 238 | 0 |

The two gates disagree on about 70 offers. The LLM catches what patterns cannot ("Software Engineer II",
"approx. 2 years", "a small, senior team", Polish "3-letnie doświadczenie"); the regex removes junior roles
for a word ("Junior Product Manager" for "manager"). The fix for the LLM's one miss is deterministic and
covered by tests, so it costs no new calls. One trap found on the way: a duty such as "mentor a junior
engineer" contains "junior" but describes a senior role, so the range rule reads only the title and
explicit ranges.

| judged pool (102) | AUC | nDCG@100 |
|---|---|---|
| vector + regex gate | 0.769 | 0.715 |
| vector + LLM gate | **0.785** | 0.727 |
| + excluding sales and design roles | 0.767 | 0.717 |

Excluding role families did not help: vector search already ranks sales and design ads low, so the extra
rule mostly removes offers that were not near the top anyway (and one applied offer). The gate's value is
level and years.

## Round 3: LLM scoring with checked quotes

The scorer (`score.py`) asks the model to judge each requirement of the ad as met, partial or missing and
to quote the candidate's evidence word for word. Code then checks every quote against the evidence file:
a "met" without a real quote drops to "partial", a "partial" without one to "missing", a quote from
coursework counts as "partial" at most. Code computes the score (70 must-haves, 15 nice-to-haves, 10 level,
5 domain, dealbreaker caps at 40), the same formula as the earlier judging script, so the two compare.
Evidence: about 15k tokens per call.

| scorer on the judged pool (102 offers, 53 applied) | AUC | R@20 | nDCG@100 | quotes not found | cost per 100 ads |
|---|---|---|---|---|---|
| Gemini 3.5 Flash Lite, prompt v1 (78 scored, rest hit the daily limit) | 0.589 | 0.263 | 0.669 | 30% | free tier |
| Claude Haiku via subscription CLI, prompt v1 | 0.738 | 0.321 | 0.733 | 7% | $0.31 API-equivalent |
| Claude Haiku, prompt v2 | 0.807 | 0.346 | 0.786 | 7% | $0.34 API-equivalent |
| earlier judging pipeline (reference, inflated) | 0.863 | 0.377 | 0.829 | | |

What changed from v1 to v2, found by reading the largest disagreements with the reference:

- **An empty ad scored 93.** One ad's text was just "xx"; the model invented requirements and met them.
  Ads under 300 characters are now skipped and reported, never scored.
- **Dealbreakers were miscalibrated.** v1 capped an offer at 40 for one missing required skill (Java in a
  Python-and-Java QA role), while the author applied to such roles: juniors get hired with gaps. v2 defines
  a dealbreaker as one of four things: above entry level, a main stack the evidence never shows, a foreign
  core domain, or a legal or language requirement.

v2 was written after looking at this pool, so its gain is optimistic; the next labelled offers are the
real test.

### Combining retrieval with the scorer

| judged pool | AUC | R@20 | R@50 | nDCG@100 |
|---|---|---|---|---|
| vector + LLM gate | 0.785 | 0.264 | 0.717 | 0.727 |
| Haiku v2 score | 0.796 | 0.340 | 0.698 | 0.777 |
| **RRF(vector, Haiku v2)** | **0.851** | 0.340 | 0.755 | 0.792 |

The two signals are complementary. Embeddings say whether an offer is in the candidate's direction;
the scorer says whether the candidate meets its requirements. Fused, they come within about 0.01 AUC of the
reference judge, which needed a stronger model per ad and whose number is inflated by the selection bias
described above.

Takeaways for the design: Flash Lite is good enough to extract fields from an ad and not good enough to
judge fit (it invents quotes); a small Claude model judges well at about a third of a cent per ad; and
the scorer should be fused with retrieval, not replace it.

## Round 4: Haiku for everything, and the whole funnel

Gemini's free tier gave about six calls a minute and judged poorly, so the default model is now Claude
Haiku through the subscription CLI (`claude -p --json-schema`). Extraction of all 1,266 ads with text took
16 minutes with six processes, with no failures (Gemini: 557 ads in two hours, with failures).

Haiku as the gate's extractor removes 548 offers (Gemini 238 of 542, regex 522 of 1,274) and one applied
offer: a senior-flavoured mobile role at a large AI company, read as senior from "highly self-directed".
It infers level from tone more readily than Gemini did.

### The funnel over all 1,532 offers

Retrieval (vector, both CVs) and the gate first; Haiku v2 then scores the top N; the rest keep their
retrieval order.

| ranking | R@20 | R@50 | R@100 | R@200 | nDCG@100 | Haiku calls |
|---|---|---|---|---|---|---|
| retrieval + gate | 0.052 | 0.167 | 0.323 | 0.562 | 0.250 | 0 |
| score top 50 | 0.135 | 0.167 | 0.323 | 0.562 | 0.336 | 50 |
| score top 150 | 0.094 | 0.281 | 0.396 | 0.562 | 0.382 | 150 |
| score top 200 | 0.104 | 0.281 | 0.500 | 0.562 | 0.435 | 200 |
| **score top 300** | 0.125 | **0.292** | **0.562** | 0.750 | **0.486** | 300 |
| score top 500 | 0.135 | 0.271 | 0.521 | 0.812 | 0.480 | 500 |
| score everything that passes the gate (878) | 0.125 | 0.260 | 0.479 | 0.760 | 0.453 | 878 |
| RRF(retrieval, score), top 300 | 0.115 | 0.250 | 0.448 | 0.740 | 0.421 | 300 |

Scoring the top 300 doubles nDCG over retrieval alone. API-equivalent cost: about $0.30 per 100 ads
(scoring), about $0.13 per 100 (extraction); on the subscription both are free.

**Scoring everything is worse than scoring the top 300.** Retrieval does more than save calls: offers far
from the candidate's direction can still score well on generic requirements (Python, Git, English), and the
retrieval cut keeps them out. The best setting uses about a third of the calls of the brute-force one.

**Fusion helps on the judged pool and hurts on the full one.** In the judged pool every offer was
plausible, so the two signals were close in strength and complemented each other (AUC 0.851). Over all
offers the retrieval order below the top is noisy and fusing it back in dilutes the scorer. The ranking
in the product is therefore: gate, retrieval cut at 300, then the score.

## Next

- Recall for the funnel: union of CV-vector and keyword candidates at a fixed budget (100, 200, 300).
- A ranking command (`jm rank`) that runs the chosen funnel on new offers and prints a short list with reasons.
- Agent with tools and an MCP server over the database.
- FastAPI + web page with precomputed results and live vector search; Docker; Azure and AWS with Terraform.
