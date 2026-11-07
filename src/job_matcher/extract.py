"""LLM extraction of the fields the gate and the scorer need, with a quote from the ad for the
fields that decide whether an offer passes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from job_matcher.llm import ChatModel

RoleFamily = Literal[
    "backend", "frontend", "fullstack", "mobile", "data_engineering", "data_analysis", "ml_ai",
    "qa_testing", "devops_sre", "it_support_ops", "security", "embedded", "product_design",
    "sales_marketing", "other_business",
]


class Extraction(BaseModel):
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "unknown"] = Field(
        description="Level the ad hires for. Graduate programmes and trainees count as intern or junior. "
                    "'unknown' only if the ad gives no level, no years and no hint.")
    seniority_quote: str = Field(description="Short verbatim quote from the ad that shows the level; '' if none")
    min_years: int | None = Field(description="Smallest number of years of professional experience the ad "
                                              "requires; null if it names none. Not the company's age.")
    years_quote: str = Field(description="Verbatim quote with the years requirement; '' if none")
    role_family: RoleFamily
    must_have: list[str] = Field(description="Required skills or tools, short names, at most 10")
    nice_to_have: list[str] = Field(description="Optional skills or tools, short names, at most 10")
    languages_required: list[str] = Field(description="Human languages the ad requires, e.g. ['Polish', 'English']")
    work_mode: Literal["remote", "hybrid", "office", "unknown"]


SYSTEM = (
    "You read job ads (Polish or English) and return the requested fields. Use only what the ad says; "
    "quotes must be copied from the ad exactly. Do not guess a level from the company or the salary."
)


def ad_text(title: str, company: str, description: str, limit: int = 12000) -> str:
    return f"Title: {title}\nCompany: {company}\n\n{description[:limit]}"


def extract_offers(conn, model: ChatModel, ids: list[str] | None = None, workers: int = 8,
                   progress: bool = True) -> tuple[int, int]:
    sql = """SELECT id, title, company, description FROM offers o WHERE description <> ''
             AND NOT EXISTS (SELECT 1 FROM extractions x WHERE x.offer_id = o.id AND x.extractor = %s)"""
    params: list = [model.name]
    if ids is not None:
        sql += " AND id = ANY(%s)"
        params.append(ids)
    rows = conn.execute(sql, params).fetchall()
    done = failed = 0

    def one(row):
        oid, title, company, desc = row
        return oid, model.parse(SYSTEM, ad_text(title, company, desc), Extraction)

    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(one, r) for r in rows]
        for f in as_completed(futures):
            try:
                oid, ex = f.result()
            except Exception as e:  # keep going; a failed offer is retried on the next run
                failed += 1
                print("failed:", str(e)[:160])
                continue
            conn.execute("INSERT INTO extractions (offer_id, extractor, data) VALUES (%s, %s, %s) "
                         "ON CONFLICT (offer_id, extractor) DO UPDATE SET data = EXCLUDED.data, created_at = now()",
                         (oid, model.name, Jsonb(ex.model_dump())))
            done += 1
            if progress and done % 50 == 0:
                print(f"  {done}/{len(rows)}  calls={model.usage.calls} in={model.usage.input_tokens} "
                      f"out={model.usage.output_tokens}", flush=True)
    return done, failed
