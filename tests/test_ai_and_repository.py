from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from unittest.mock import Mock

import pytest
from fakes import CONFIG

from auditable_crud.ai_sql import build_prompt, extract_sql
from auditable_crud.config import TableConfig
from auditable_crud.diff import ChangeKind
from auditable_crud.repository import (
    ADMIN_API,
    DEVELOPER_API,
    PAGE_SIZE,
    ApiError,
    Repository,
    core_schema_for,
)
from auditable_crud.runner import Row
from auditable_crud.sql_guard import GuardError, TableRef, guard_dml

TARGET = TableRef("APP", "DEMO", "PRICING_CONFIG")


@pytest.mark.parametrize(
    "response",
    [
        "```sql\nUPDATE pricing_config SET price = 0 WHERE sku = 'A';\n```",
        "Sure! Here you go:\n\nUPDATE pricing_config SET price = 0 WHERE sku = 'A';",
        "\"```sql\\nUPDATE pricing_config SET price = 0 WHERE sku = 'A';\\n```\"",
    ],
)
def test_extract_sql_from_typical_model_answers(response: str) -> None:
    sql = extract_sql(response)
    assert guard_dml(sql, TARGET).kind == "UPDATE"


def test_extract_sql_returns_empty_for_refusals() -> None:
    assert extract_sql("I cannot help with that.") == ""


def test_json_answers_preserve_unicode_and_backslashes_in_sql_literals() -> None:
    statement = r"UPDATE pricing_config SET label = 'Preço 東京 🥑', path = 'a\\b'"
    assert extract_sql(json.dumps(statement, ensure_ascii=False)) == statement
    assert extract_sql(json.dumps(statement, ensure_ascii=True)) == statement


def test_multiple_proposals_cannot_silently_replace_one_another() -> None:
    blocks = "```sql\nUPDATE pricing_config SET price=10\n```"
    assert extract_sql(blocks + "\nAlternative:\n" + blocks) == ""
    assert extract_sql(blocks + "\nUPDATE pricing_config SET price=20") == ""


def test_multi_statement_answers_are_caught_by_the_guard() -> None:
    sql = extract_sql("```sql\nDELETE FROM pricing_config; DROP TABLE pricing_config;\n```")
    with pytest.raises(GuardError, match="Exactly one"):
        guard_dml(sql, TARGET)


def test_prompt_contains_schema_sample_and_rules() -> None:
    prompt = build_prompt(
        TARGET,
        [("SKU", "TEXT"), ("PRICE", "NUMBER")],
        [{"SKU": "A", "PRICE": None}] * 10,
        "  raise EU prices by 10%  ",
        ["SKU"],
    )
    assert "Target table: APP.DEMO.PRICING_CONFIG" in prompt
    assert "  PRICE NUMBER" in prompt
    assert prompt.count("A | NULL") == 5  # sample capped
    assert prompt.endswith("raise EU prices by 10%\n")


class Recorder:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, list[Any]]] = []

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        self.calls.append((sql, list(params)))
        return self.rows


def test_call_binds_operation_and_json_args() -> None:
    runner = Recorder([{DEVELOPER_API: json.dumps({"ok": True, "result": {"request_id": "r"}})}])
    repo = Repository(runner, core_schema_for("APP"), DEVELOPER_API)
    assert repo.call("submit", request_id="r", actor="x'y") == {"request_id": "r"}
    sql, params = runner.calls[0]
    assert sql == 'CALL "APP"."CORE".SP_DEV_API(?, PARSE_JSON(?))'
    assert params == ["submit", '{"request_id": "r", "actor": "x\'y"}']


def test_call_raises_api_error() -> None:
    runner = Recorder([{ADMIN_API: {"ok": False, "error": "nope"}}])
    repo = Repository(runner, '"APP"."CORE"', ADMIN_API)
    with pytest.raises(ApiError, match="nope"):
        repo.call("execute", request_id="r", actor="a")
    assert runner.calls[0][0] == 'CALL "APP"."CORE".SP_ADMIN_API(?, PARSE_JSON(?))'


def test_repository_requires_one_of_the_application_entry_points() -> None:
    with pytest.raises(ValueError, match="Unknown API procedure"):
        Repository(Recorder([]), '"APP"."CORE"', "SP_MISSING")


def test_request_filters_are_bound_not_interpolated() -> None:
    runner = Recorder([])
    Repository(runner, '"APP"."CORE"', DEVELOPER_API).requests(
        requested_by="o'brien", statuses=["SUBMITTED", "APPROVED"]
    )
    sql, params = runner.calls[0]
    assert "o'brien" not in sql
    assert params == ["o'brien", "SUBMITTED", "APPROVED"]


def test_empty_request_list_still_has_its_columns() -> None:
    """Pages filter on STATUS/REQUESTED_BY; an empty result must not drop the columns."""
    frame = Repository(Recorder([]), '"APP"."CORE"', ADMIN_API).requests(statuses=["SUBMITTED"])
    assert frame.empty
    assert {"REQUEST_ID", "STATUS", "REQUESTED_BY", "TABLE_ID"} <= set(frame.columns)


def test_legacy_successful_request_does_not_show_a_null_error() -> None:
    runner = Recorder([{"STATUS": "DRY_RUN_OK", "LAST_ERROR": "None", "SUBMIT_NOTE": "None"}])
    request = Repository(runner, core_schema_for("APP"), DEVELOPER_API).request("r")
    assert request["LAST_ERROR"] is None and request["SUBMIT_NOTE"] == "None"


def test_table_remapping_is_visible_in_an_open_session() -> None:
    runner = Recorder([dict(CONFIG)])
    repo = Repository(runner, '"APP"."CORE"', DEVELOPER_API)
    assert repo.tables()[0].source.name == CONFIG["SOURCE_TABLE"]
    runner.rows = [{**CONFIG, "SOURCE_TABLE": "NEW_SOURCE"}]
    assert repo.tables()[0].source.name == "NEW_SOURCE"


def test_diff_page_uses_offset_paging() -> None:
    runner = Recorder([{"SKU": "A"}])
    frame = Repository(runner, '"APP"."CORE"', ADMIN_API).diff_page(
        TARGET, ChangeKind.DELETED, ["SKU"], 2
    )
    assert list(frame["SKU"]) == ["A"]
    assert runner.calls[0][1] == [PAGE_SIZE, 2 * PAGE_SIZE]


def test_update_page_and_histogram_preserve_exact_source_labels() -> None:
    # The first two identifiers share their first 254 characters. Other names
    # resemble internal aliases, SQL keywords and before/after display labels.
    columns = [
        "X" * 254 + "A",
        "X" * 254 + "B",
        "B0",
        "A0",
        "C0",
        "SELECT",
        'a"b',
        "Price",
        "price",
        "price (after)",
        "Changed columns",
    ]
    row: Row = {}
    expected: Row = {}
    for index, name in enumerate(columns):
        row[f"B{index}"] = expected[f"{name} (before)"] = index
        row[f"A{index}"] = expected[f"{name} (after)"] = index + 1
    runner = Recorder([dict(reversed(row.items()))])
    repo = Repository(runner, core_schema_for("APP"), ADMIN_API)
    frame = repo.diff_page(TARGET, ChangeKind.UPDATED, columns)
    assert list(frame.columns) == list(expected)
    assert frame.iloc[0].to_dict() == expected
    assert 'd."a""b" AS B6' in runner.calls[-1][0] and 'd."a""b" AS A6' in runner.calls[-1][0]
    assert " (before)" not in runner.calls[-1][0]
    runner.rows = [{f"C{i}": i for i in range(len(columns))}]
    assert repo.column_changes(TARGET, columns) == {name: i for i, name in enumerate(columns)}
    runner.rows = []
    assert list(repo.diff_page(TARGET, ChangeKind.UPDATED, columns)) == list(expected)


def test_drift_page_preserves_columns_named_like_the_overlap_flag() -> None:
    columns = ["SKU", "AC_OVERLAPS", "AC_OVERLAPS_1", "ac_overlaps_2"]
    initial = ["SKU", "OLD_COLUMN"]
    source = dict.fromkeys(columns, "business value")
    runner = Mock(spec=Recorder)
    runner.query.side_effect = [
        [{"COLUMN_NAME": name, "DATA_TYPE": "TEXT"} for name in initial],
        [
            {
                "AC_ACTION": "INSERT",
                "AC_ISUPDATE": False,
                "AC_OVERLAPS": True,
                **dict.fromkeys(initial),
            }
        ],
        [{"COLUMN_NAME": name, "DATA_TYPE": "TEXT"} for name in columns],
        [{"AC_ACTION": "INSERT", "AC_ISUPDATE": True, "AC_OVERLAPS_2": False, **source}],
    ]
    repo = Repository(runner, core_schema_for("APP"), ADMIN_API)
    cfg = TableConfig.from_row(CONFIG)
    first = repo.drift_rows(cfg, 1_700_000_000_000_000_000, TARGET)
    assert "OLD_COLUMN" in first and first.attrs["overlap_column"] == "AC_OVERLAPS"
    frame = repo.drift_rows(
        cfg, 1_700_000_000_000_000_000, TARGET, end_ns=1_700_000_000_000_000_123
    )
    assert frame.columns.is_unique and "OLD_COLUMN" not in frame
    assert frame[columns].iloc[0].to_dict() == source
    assert not frame[frame.attrs["overlap_column"]].iloc[0]
    sql = runner.query.call_args.args[0]
    assert 'AS "AC_OVERLAPS_2", c."SKU", c."AC_OVERLAPS"' in sql
    assert "END(TIMESTAMP => TO_TIMESTAMP_LTZ(1700000000000000123, 9))" in sql
    assert 'ORDER BY "AC_OVERLAPS_2" DESC' in sql and '"OLD_COLUMN"' not in sql
