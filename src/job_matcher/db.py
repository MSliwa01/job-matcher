import os
from importlib.resources import files

import psycopg
from pgvector.psycopg import register_vector

DEFAULT_URL = "postgresql://jm:jm@127.0.0.1:5433/jobs"


def connect() -> psycopg.Connection:
    conn = psycopg.connect(os.environ.get("DATABASE_URL", DEFAULT_URL), autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)
    return conn


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute((files("job_matcher") / "schema.sql").read_text())
