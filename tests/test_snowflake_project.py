"""Protect active DEV data when a candidate setup or live test fails."""

from unittest.mock import Mock

import pytest
from snowflake.snowpark import Session
from snowflake_project import workflow_schema


@pytest.mark.parametrize("fail_setup", [True, False])
def test_isolated_run_tears_down_only_its_own_schema_on_failure(
    monkeypatch: pytest.MonkeyPatch, fail_setup: bool
) -> None:
    monkeypatch.setenv("AC_ISOLATED_TESTS", "1")
    monkeypatch.setenv("AC_CORE_SCHEMA", "SHARED_SCHEMA")
    session = Mock(spec=Session)
    queries: list[str] = []

    def query(sql: str) -> Mock:
        queries.append(sql)
        if fail_setup and sql.startswith("CREATE PROCEDURE"):
            raise RuntimeError("fixture failed")
        return Mock()

    session.sql.side_effect = query
    with (
        pytest.raises(RuntimeError, match="fixture failed"),
        workflow_schema(session, "APP") as schema,
    ):
        assert not fail_setup and schema.startswith("AC_PR_REVIEW_")
        raise RuntimeError("fixture failed")
    owned = queries[0].removeprefix("CREATE TRANSIENT SCHEMA ")
    assert owned.startswith('"APP"."AC_PR_REVIEW_')
    assert queries[-1] == f"DROP SCHEMA {owned}"
    assert all(owned in sql and "SHARED_SCHEMA" not in sql for sql in queries)
    procedures = [sql for sql in queries if sql.startswith("CREATE PROCEDURE")]
    assert procedures and all(f"_CORE_SCHEMA = '{owned}'" in sql for sql in procedures)
