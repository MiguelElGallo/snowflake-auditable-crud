import os
from pathlib import Path

import pytest
import sqlglot
from jinja2 import Environment, StrictUndefined
from snowflake.snowpark import Session
from sqlglot import exp

from auditable_crud.naming import sql_string_literal

VIEWS = Path(__file__).resolve().parents[1] / "sources/definitions/views.sql"
CONNECTION = os.environ.get("AC_SNOWFLAKE_CONNECTION")
DESCRIPTION = r"C:\new\O'Brien's files"
TABLE = {
    "table_id": " \tfIlEs\n",
    "source_database": "APP",
    "source_schema": "SOURCE",
    "source_table": "FILES",
    "key_columns": "ID",
    "description": DESCRIPTION,
}


def _config_select(tables: list[dict[str, str]]) -> str:
    env = Environment(undefined=StrictUndefined, autoescape=False)  # noqa: S701 - Snowflake SQL
    template = env.from_string(VIEWS.read_text())
    rendered = template.render(app_db="APP", managed_tables=tables)
    definition = rendered.split("DEFINE VIEW APP.CORE.V_REQUESTS", 1)[0]
    return definition.split("\nAS\n", 1)[1].strip().removesuffix(";")


def test_empty_configuration_keeps_the_named_columns() -> None:
    query = sqlglot.parse_one(_config_select([]), read="snowflake", into=exp.Select)
    assert query.named_selects == [
        "TABLE_ID",
        "SOURCE_DATABASE",
        "SOURCE_SCHEMA",
        "SOURCE_TABLE",
        "CLONE_DATABASE",
        "CLONE_SCHEMA",
        "KEY_COLUMNS",
        "ALLOWED_READ_TABLES",
        "MAX_AFFECTED_ROWS",
        "DESCRIPTION",
        "AI_MODEL",
    ]
    assert query.args["where"].this == exp.false()


def test_configuration_preserves_apostrophes_and_backslashes() -> None:
    sql = _config_select([TABLE])
    assert sql_string_literal(DESCRIPTION) in sql
    literals = sqlglot.parse_one(sql, read="snowflake").find_all(exp.Literal)
    assert any(literal.this == DESCRIPTION for literal in literals)


def test_manifest_id_matches_the_app_after_whitespace_and_case_normalization() -> None:
    select = sqlglot.parse_one(_config_select([TABLE]), read="snowflake")
    values = select.find(exp.Values)
    assert values is not None
    assert values.expressions[0].expressions[0].this == "FILES"


@pytest.mark.snowflake
@pytest.mark.skipif(not CONNECTION, reason="set AC_SNOWFLAKE_CONNECTION to run")
def test_snowflake_reads_empty_and_configured_manifests() -> None:
    session = (
        Session.builder.config("connection_name", CONNECTION).config("role", "AC_OWNER").create()
    )
    try:
        empty = session.sql(_config_select([]))
        assert empty.collect() == []
        configured = session.sql(_config_select([TABLE]))
        assert empty.columns == configured.columns
        assert [type(field.datatype) for field in empty.schema.fields] == [
            type(field.datatype) for field in configured.schema.fields
        ]
        (row,) = configured.collect()
        assert row["DESCRIPTION"] == DESCRIPTION
        assert row["TABLE_ID"] == "FILES" and row["MAX_AFFECTED_ROWS"] == 10000
    finally:
        session.close()
