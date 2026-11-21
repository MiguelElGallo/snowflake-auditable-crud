"""Install the candidate workflow in a fresh transient schema for live testing."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from uuid import uuid4

from jinja2 import Environment, StrictUndefined
from snowflake.connector.util_text import split_statements
from snowflake.snowpark import Session

from auditable_crud.naming import normalize_identifier, quote_identifier

DEFINITIONS = Path(__file__).resolve().parents[1] / "sources/definitions"


def candidate_statements(database: str, schema: str) -> Iterator[str]:
    database, schema = normalize_identifier(database), normalize_identifier(schema)
    target = f"{quote_identifier(database)}.{quote_identifier(schema)}"
    tables = [
        {
            "table_id": "Pricing_Config",
            "source_table": "PRICING_CONFIG",
            "key_columns": "SKU,REGION",
            "allowed_read_tables": f"{database}.{schema}.REGIONS",
        },
        {"table_id": "DEFAULTS_DEMO", "source_table": "DEFAULTS_DEMO", "key_columns": "ID"},
    ]
    for table in tables:
        table.update(
            source_database=database,
            source_schema=schema,
            clone_database=database,
            clone_schema=schema,
        )
    yield (
        f"CREATE TABLE {target}.DEFAULTS_DEMO (ID INT, LABEL VARCHAR DEFAULT 'original') "
        "CHANGE_TRACKING = TRUE"
    )
    env = Environment(undefined=StrictUndefined, autoescape=False)  # noqa: S701 - Snowflake SQL
    for filename in ("core_tables.sql", "demo.sql", "views.sql", "procedures.sql"):
        template = (DEFINITIONS / filename).read_text()
        for shared in ("CORE", "DEMO"):
            template = template.replace("{{ app_db }}." + shared + ".", target + ".")
        template = template.replace('"{{ app_db }}"."CORE"', target)
        template = re.sub(r"^DEFINE (TABLE|VIEW|PROCEDURE) ", r"CREATE \1 ", template, flags=re.M)
        rendered = env.from_string(template).render(app_db=database, managed_tables=tables)
        for statement, _ in split_statements(StringIO(rendered), remove_comments=True):
            if not re.match(
                r"CREATE (?:TABLE|VIEW|PROCEDURE) " + re.escape(target + "."), statement
            ):
                raise ValueError(f"Unexpected DDL in {filename}: {statement[:120]}")
            yield statement


@contextmanager
def workflow_schema(session: Session, database: str) -> Iterator[str]:
    if os.environ.get("AC_ISOLATED_TESTS") != "1":
        yield normalize_identifier(os.environ.get("AC_CORE_SCHEMA", "CORE"))
        return
    schema = "AC_PR_REVIEW_" + uuid4().hex.upper()
    target = f"{quote_identifier(database)}.{quote_identifier(schema)}"
    session.sql(f"CREATE TRANSIENT SCHEMA {target}").collect()
    try:
        for statement in candidate_statements(database, schema):
            session.sql(statement).collect()
        yield schema
    finally:
        session.sql(f"DROP SCHEMA {target}").collect()
