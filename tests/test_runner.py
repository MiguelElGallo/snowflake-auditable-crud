"""SnowparkRunner must hand Snowpark only parameters it can bind."""

from __future__ import annotations

from typing import Any

from fakes import check_bindable

from auditable_crud.runner import SnowparkRunner
from auditable_crud.state import Status


class _Row:
    def as_dict(self) -> dict[str, Any]:
        return {"A": 1}


class _Session:
    """Rejects unbindable parameters the way the Snowflake connector does."""

    def __init__(self) -> None:
        self.params: list[Any] | None = []
        self.statement_params: dict[str, str] | None = None

    def sql(self, sql: str, params: list[Any] | None = None) -> _Session:
        check_bindable(params or ())
        self.params = params
        return self

    def collect(self, *, statement_params: dict[str, str] | None = None) -> list[_Row]:
        self.statement_params = statement_params
        return [_Row()]


def test_enum_parameters_are_sent_as_their_values() -> None:
    session = _Session()
    rows = SnowparkRunner(session).query("SELECT ?, ?", (Status.SUBMITTED, 3))
    assert session.params == ["SUBMITTED", 3]
    assert type(session.params[0]) is str
    assert rows == [{"A": 1}]


def test_no_parameters_are_sent_as_none() -> None:
    session = _Session()
    SnowparkRunner(session).query("SELECT 1")
    assert session.params is None


def test_only_guarded_dml_receives_a_fresh_copy_of_the_workflow_context() -> None:
    session = _Session()
    runner = SnowparkRunner(session)
    assert runner.query_dml("UPDATE config SET active = FALSE") == [{"A": 1}]
    first = session.statement_params
    assert first is not None
    assert first["DATE_INPUT_FORMAT"] == "YYYY-MM-DD"
    assert first["TIMEZONE"] == "UTC" and first["TIMESTAMP_TYPE_MAPPING"] == "TIMESTAMP_NTZ"
    assert first["WEEK_START"] == first["WEEK_OF_YEAR_POLICY"] == "0"
    assert (
        first["ERROR_ON_NONDETERMINISTIC_UPDATE"]
        == first["ERROR_ON_NONDETERMINISTIC_MERGE"]
        == "TRUE"
    )
    first["_PLAN_UUID"] = "per-statement Snowpark metadata"
    runner.query_dml("UPDATE config SET active = TRUE")
    assert session.statement_params is not None and "_PLAN_UUID" not in session.statement_params
    runner.query("SELECT 1")
    assert session.statement_params is None
