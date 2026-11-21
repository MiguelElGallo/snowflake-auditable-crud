import sqlite3

import pytest
import sqlglot

from auditable_crud.config import TableConfig
from auditable_crud.diff import (
    ChangeKind,
    DiffCounts,
    changed_columns,
    column_changes_sql,
    counts_sql,
    fingerprint_sql,
    map_column_changes,
    materialize_sql,
    page_sql,
)
from auditable_crud.drift import (
    DriftClass,
    DriftReport,
    build_report,
    drift_counts_sql,
    drift_overlap_sql,
    drift_rows_sql,
)
from auditable_crud.naming import IdentifierError
from auditable_crud.sql_guard import TableRef

DIFF = '"APP"."SANDBOX"."T__CR_DIFF"'
SRC = '"APP"."DEMO"."T"'
COLS = ["SKU", "PRICE", "weird col"]


def _parses(sql: str) -> None:
    sqlglot.parse_one(sql.replace("?", "1"), read="snowflake")


def test_dml_result_parsing_handles_merge_and_single_counts() -> None:
    merge = {"number of rows inserted": 2, "number of rows updated": 5, "number of rows deleted": 1}
    assert DiffCounts.from_dml_result(merge) == DiffCounts(2, 5, 1)
    assert DiffCounts.from_dml_result({"number of rows updated": 7}).as_dict() == {
        "inserted": 0,
        "updated": 7,
        "deleted": 0,
        "total": 7,
    }


def test_dml_result_ignores_multi_joined_rows_column() -> None:
    update = {"number of rows updated": 4, "number of multi-joined rows updated": 4}
    assert DiffCounts.from_dml_result(update) == DiffCounts(0, 4, 0)


@pytest.mark.parametrize(
    "sql",
    [
        materialize_sql(DIFF, '"APP"."SANDBOX"."T__CR_STRM"'),
        counts_sql(DIFF),
        fingerprint_sql(DIFF, COLS),
        page_sql(DIFF, ChangeKind.UPDATED, COLS),
        page_sql(DIFF, ChangeKind.INSERTED, COLS),
        column_changes_sql(DIFF, COLS),
        drift_counts_sql(SRC, 1_700_000_000_000_000_000),
        drift_overlap_sql(SRC, 1_700_000_000_000_000_000, DIFF, ["SKU"]),
        drift_rows_sql(SRC, 1_700_000_000_000_000_000, DIFF, ["SKU", "REGION"]),
    ],
)
def test_generated_sql_is_valid_snowflake(sql: str) -> None:
    _parses(sql)


def test_update_page_pairs_before_and_after_with_quoted_columns() -> None:
    sql = page_sql(DIFF, ChangeKind.UPDATED, COLS)
    assert 'd."weird col" AS B2' in sql and 'd."weird col" AS A2' in sql
    assert "b.B2, a.A2" in sql
    assert "AC_ISUPDATE" in sql


def test_deleted_page_excludes_update_halves() -> None:
    sql = page_sql(DIFF, ChangeKind.DELETED, COLS)
    assert "AC_ACTION = 'DELETE' AND NOT AC_ISUPDATE" in sql


def test_preview_pages_show_the_actual_stream_values_in_order() -> None:
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE changes (SKU TEXT, PRICE INT, AC_ROW_ID INT, "
            "AC_ACTION TEXT, AC_ISUPDATE INT)"
        )
        db.executemany(
            "INSERT INTO changes VALUES (?, ?, ?, ?, ?)",
            [
                ("B", None, 2, "INSERT", True),
                ("Removed", 5, 4, "DELETE", False),
                ("A", 11, 1, "INSERT", True),
                ("B", 20, 2, "DELETE", True),
                ("New", 15, 3, "INSERT", False),
                ("A", 10, 1, "DELETE", True),
            ],
        )
        updated = page_sql("changes", ChangeKind.UPDATED, ["SKU", "PRICE"])
        assert db.execute(updated, [2, 0]).fetchall() == [
            ("A", "A", 10, 11),
            ("B", "B", 20, None),
        ]
        assert db.execute(updated, [1, 1]).fetchall() == [("B", "B", 20, None)]
        for kind, expected in [
            (ChangeKind.INSERTED, ("New", 15)),
            (ChangeKind.DELETED, ("Removed", 5)),
        ]:
            assert db.execute(page_sql("changes", kind, ["SKU", "PRICE"]), [10, 0]).fetchall() == [
                expected
            ]


def test_column_change_mapping_and_row_comparison() -> None:
    assert map_column_changes({"C0": 0, "C1": 4, "C2": None}, COLS) == {
        "SKU": 0,
        "PRICE": 4,
        "weird col": 0,
    }
    assert changed_columns({"SKU": "A", "PRICE": 1}, {"SKU": "A", "PRICE": 2}, COLS) == ["PRICE"]


def test_drift_classification() -> None:
    assert DriftReport(1, 2, DIFF, 0, 0, 0, 0).classification is DriftClass.NONE
    assert DriftReport(1, 2, DIFF, 3, 0, 0, 0).classification is DriftClass.DISJOINT
    counts = {"INSERTED": 1, "UPDATED": 2, "DELETED": None}
    report = build_report(1, counts, {"OVERLAPPING_KEYS": 1}, end_ns=2, diff_fqn=DIFF)
    assert report.classification is DriftClass.OVERLAP
    assert report.as_dict()["total"] == 3


@pytest.mark.parametrize("bad", [0, -5, True, "1 OR 1=1"])
def test_drift_baseline_must_be_positive_int(bad: object) -> None:
    with pytest.raises(ValueError, match="baseline_ns"):
        drift_counts_sql(SRC, bad)  # type: ignore[arg-type]


def test_overlap_needs_keys() -> None:
    with pytest.raises(ValueError, match="key column"):
        drift_overlap_sql(SRC, 1, DIFF, [])


def test_overlap_shortens_composite_keys_before_joining() -> None:
    columns = ["X" * 254 + "A", "X" * 254 + "B"]
    sql = drift_overlap_sql(SRC, 1, DIFF, columns)
    assert f's."{columns[0]}" AS D0' in sql and f's."{columns[1]}" AS T1' in sql
    assert "EQUAL_NULL(d.D0, t.T0) AND EQUAL_NULL(d.D1, t.T1)" in sql


ROW = {
    "TABLE_ID": "pricing_config",
    "SOURCE_DATABASE": "app",
    "SOURCE_SCHEMA": "demo",
    "SOURCE_TABLE": "pricing_config",
    "CLONE_DATABASE": "app",
    "CLONE_SCHEMA": "sandbox",
    "KEY_COLUMNS": "sku, region",
    "ALLOWED_READ_TABLES": "app.demo.regions",
    "MAX_AFFECTED_ROWS": 500,
    "DESCRIPTION": "Prices",
    "AI_MODEL": None,
}


def test_config_row_is_normalized() -> None:
    cfg = TableConfig.from_row(ROW)
    assert cfg.source == TableRef("APP", "DEMO", "PRICING_CONFIG")
    assert cfg.key_columns == ("SKU", "REGION")
    assert cfg.allowed_reads == frozenset({TableRef("APP", "DEMO", "REGIONS")})
    assert cfg.sandbox_ref("X").sql == '"APP"."SANDBOX"."X"'
    assert cfg.ai_model == "claude-sonnet-4-5"


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"KEY_COLUMNS": ""}, ValueError),
        ({"CLONE_SCHEMA": "x; drop"}, IdentifierError),
        ({"MAX_AFFECTED_ROWS": -1}, ValueError),
        ({"MAX_AFFECTED_ROWS": 0}, ValueError),
    ],
)
def test_config_rejects_bad_rows(override: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        TableConfig.from_row({**ROW, **override})
