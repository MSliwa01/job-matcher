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

## Next

- Recall for the funnel: union of CV-vector and keyword candidates at a fixed budget (100, 200, 300).
- LLM extraction of level, years and role family to replace the regex gate, measured against it.
- LLM scoring on the funnel's candidates; compare the API model with the free local mode.
