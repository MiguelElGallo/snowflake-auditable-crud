"""Tests for the SQL-backed request store (statement shapes and parameter binding)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

import pytest

from auditable_crud.runner import Row
from auditable_crud.state import Status, WorkflowError
from auditable_crud.store import NOW, SqlRequestStore

CORE = '"APP"."CORE"'


class Recorder:
    def __init__(self, rows: list[Row] | None = None) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.rows = rows if rows is not None else []

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        self.calls.append((sql, list(params)))
        return self.rows


def test_config_lookup_matches_the_app_identifier_and_rejects_ambiguous_ids() -> None:
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE V_MANAGED_TABLES (TABLE_ID TEXT, SOURCE_TABLE TEXT)")
        db.execute("INSERT INTO V_MANAGED_TABLES VALUES (' Feature_Flags ', 'FLAGS')")
        db.row_factory = sqlite3.Row

        class ConfigReader:
            def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
                return [dict(row) for row in db.execute(sql, params)]

        store = SqlRequestStore(ConfigReader(), "main")
        assert store.load_config("FEATURE_FLAGS")["SOURCE_TABLE"] == "FLAGS"
        assert store.load_config("feature_flags")["SOURCE_TABLE"] == "FLAGS"
        db.execute("INSERT INTO V_MANAGED_TABLES VALUES ('FEATURE_FLAGS', 'OTHER_FLAGS')")
        with pytest.raises(WorkflowError, match="more than once"):
            store.load_config("FEATURE_FLAGS")
        with pytest.raises(WorkflowError, match="not configured"):
            store.load_config("UNLISTED")


def test_update_is_compare_and_set_with_bound_values() -> None:
    runner = Recorder([{"number of rows updated": 1}])
    store = SqlRequestStore(runner, CORE)
    updated = store.update(
        "r1", {"STATUS": "APPROVED", "DRY_RUN_COUNTS": {"updated": 3}}, "SUBMITTED"
    )
    sql, params = runner.calls[0]
    assert updated == 1
    assert sql == (
        f"UPDATE {CORE}.CHANGE_REQUESTS SET STATUS = ?, DRY_RUN_COUNTS = PARSE_JSON(?), "
        "UPDATED_AT = CURRENT_TIMESTAMP() WHERE REQUEST_ID = ? AND STATUS = ?"
    )
    assert params == ["APPROVED", '{"updated": 3}', "r1", "SUBMITTED"]


def test_insert_binds_everything_and_maps_now_to_server_time() -> None:
    runner = Recorder()
    SqlRequestStore(runner, CORE).insert({"REQUEST_ID": "r'1", "CREATED_AT": NOW})
    sql, params = runner.calls[0]
    assert sql.endswith("(REQUEST_ID, CREATED_AT) SELECT ?, CURRENT_TIMESTAMP()")
    assert params == ["r'1"]


def test_enum_values_are_bound_as_plain_strings() -> None:
    """Snowpark cannot bind Enum instances (found by the Snowflake integration test)."""
    runner = Recorder([{"number of rows updated": 1}])
    SqlRequestStore(runner, CORE).update("r1", {"STATUS": Status.APPROVED}, Status.SUBMITTED)
    _, params = runner.calls[0]
    assert params == ["APPROVED", "r1", "SUBMITTED"]
    assert all(type(p) is str for p in params)


def test_nulls_are_sql_literals_and_user_text_is_bound() -> None:
    runner = Recorder([{"number of rows updated": 1}])
    store = SqlRequestStore(runner, CORE)
    store.update("r", {"LAST_ERROR": None, "SUBMIT_NOTE": "None"})
    sql, params = runner.calls[-1]
    assert "LAST_ERROR = NULL, SUBMIT_NOTE = ?" in sql
    assert params == ["None", "r"]
    store.insert({"NL_PROMPT": None, "REQUEST_ID": "r"})
    assert runner.calls[-1] == (
        f"INSERT INTO {CORE}.CHANGE_REQUESTS (NL_PROMPT, REQUEST_ID) SELECT NULL, ?",
        ["r"],
    )


def test_legacy_empty_fields_preserve_user_text_and_failed_errors() -> None:
    row = {
        "STATUS": "DRY_RUN_OK",
        "LAST_ERROR": "None",
        "DRY_RUN_FINGERPRINT": "None",
        "APPROVED_SHA256": "None",
        "NL_PROMPT": "None",
        "SUBMIT_NOTE": "None",
    }
    store = SqlRequestStore(Recorder([row]), CORE)
    decoded = store.get("r")
    assert (
        decoded["LAST_ERROR"]
        is decoded["APPROVED_SHA256"]
        is decoded["DRY_RUN_FINGERPRINT"]
        is None
    )
    assert decoded["NL_PROMPT"] == decoded["SUBMIT_NOTE"] == "None"
    row["STATUS"] = "FAILED"
    assert store.get("r")["LAST_ERROR"] == "None"


def test_column_names_are_validated() -> None:
    with pytest.raises(ValueError, match="Invalid column"):
        SqlRequestStore(Recorder(), CORE).update("r", {"STATUS = 'X'; --": 1})


def test_get_decodes_variant_json_and_reports_missing() -> None:
    store = SqlRequestStore(Recorder([{"REQUEST_ID": "r", "EXEC_COUNTS": '{"updated": 2}'}]), CORE)
    assert store.get("r")["EXEC_COUNTS"] == {"updated": 2}
    with pytest.raises(WorkflowError, match="does not exist"):
        SqlRequestStore(Recorder([]), CORE).get("missing")


def test_cleanup_scopes_filter_in_sql_and_support_zero_retention() -> None:
    runner = Recorder()
    store = SqlRequestStore(runner, CORE)
    store.cleanup_candidates(0, "demo")
    sql, params = runner.calls[-1]
    assert "REQUEST_ID = ?" in sql and "DATEADD" not in sql and params == ["demo"]
    store.idle_drafts(2, "demo")
    sql, params = runner.calls[-1]
    assert "('DRAFT', 'DRY_RUN_OK')" in sql and "DATEADD(DAY, -7" in sql
    assert "NULLIF(LAST_ERROR, 'None') IS NOT NULL" in sql
    assert params == [2, "demo"]
