"""Exercise the scheduled cleanup's response handling without running a cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from jinja2 import Template
from snowflake.snowpark import Session
from snowflake.snowpark.exceptions import SnowparkSQLException

from auditable_crud.naming import sql_string_literal

TASK = Path(__file__).resolve().parents[1] / "sources/definitions/jobs.sql"
CONNECTION = os.environ.get("AC_SNOWFLAKE_CONNECTION")


def _task_body() -> str:
    definition: str = Template(TASK.read_text(encoding="utf-8")).render(
        app_db="AUDITABLE_CRUD",
        warehouse="COMPUTE_WH",
        cleanup_retention_days=7,
        cleanup_idle_draft_days=0,
    )
    return definition.split("\nAS\n", 1)[1]


def test_scheduled_cleanup_checks_the_admin_procedure_result() -> None:
    body = _task_body()
    assert body.count("CALL AUDITABLE_CRUD.CORE.SP_ADMIN_API(") == 1
    assert "'cleanup'" in body
    assert body.index("INTO :cleanup_response;") < body.index("RAISE procedure_failed;")
    assert "RAISE sandbox_failed;" in body


@pytest.mark.snowflake
@pytest.mark.skipif(not CONNECTION, reason="set AC_SNOWFLAKE_CONNECTION to run")
def test_snowflake_task_raises_on_failed_or_incomplete_cleanup() -> None:
    body = _task_body()
    # Keep the deployed checks and exceptions. Supply a read-only synthetic result
    # in place of the mutating CALL, including malformed success envelopes.
    start = body.index("CALL AUDITABLE_CRUD.CORE.SP_ADMIN_API(")
    stop = body.index("INTO :cleanup_response;", start)
    cases = [
        ({"ok": True, "result": {"dropped": 2, "failed": 0, "expired": 0}}, None),
        ({"ok": False, "error": "unavailable"}, "PROCEDURE_FAILED"),
        ({"ok": True, "result": {"dropped": 1, "failed": 1}}, "SANDBOX_FAILED"),
        ({"ok": True, "result": {}}, "SANDBOX_FAILED"),
    ]
    session = (
        Session.builder.config("connection_name", CONNECTION).config("role", "AC_OWNER").create()
    )
    try:
        for response, exception in cases:
            literal = sql_string_literal(json.dumps(response))
            query = session.sql(body[:start] + f"SELECT PARSE_JSON({literal}) " + body[stop:])
            if exception is None:
                query.collect()
            else:
                with pytest.raises(SnowparkSQLException, match=exception):
                    query.collect()
    finally:
        session.close()
