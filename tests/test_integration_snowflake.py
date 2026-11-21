"""Exercise the deployed procedures or install candidate handlers in an isolated schema.

Set AC_SNOWFLAKE_CONNECTION to a DEV connection that can use AC_OWNER. The two
actor labels exercise the trusted app-to-procedure interface in one owner session.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from snowflake.snowpark import Session
from snowflake_project import workflow_schema
from streamlit.testing.v1 import AppTest

from auditable_crud.config import TableConfig
from auditable_crud.diff import ChangeKind
from auditable_crud.naming import information_schema, normalize_identifier, quote_identifier
from auditable_crud.repository import ADMIN_API, DEVELOPER_API, ApiError, Repository
from auditable_crud.runner import SnowparkRunner
from auditable_crud.sql_guard import TableRef
from auditable_crud.state import TERMINAL_STATUSES

CONNECTION = os.environ.get("AC_SNOWFLAKE_CONNECTION")
DEV, ADMIN = "SMOKE_DEV", "SMOKE_ADMIN"

pytestmark = [
    pytest.mark.snowflake,
    pytest.mark.skipif(not CONNECTION, reason="set AC_SNOWFLAKE_CONNECTION to run"),
]


@dataclass
class LiveApps:
    runner: SnowparkRunner
    developer: Repository
    admin: Repository
    cfg: TableConfig
    core: str


@pytest.fixture(scope="module")
def apps() -> Iterator[LiveApps]:
    database = normalize_identifier(os.environ.get("AC_APP_DATABASE", "AUDITABLE_CRUD"))
    session = (
        Session.builder.config("connection_name", CONNECTION).config("role", "AC_OWNER").create()
    )
    try:
        session.use_database(database)
        with workflow_schema(session, database) as schema:
            core = f"{quote_identifier(database)}.{quote_identifier(schema)}"
            runner = SnowparkRunner(session)
            developer = Repository(runner, core, DEVELOPER_API)
            cfg = next(t for t in developer.tables() if t.table_id == "PRICING_CONFIG")
            assert cfg.source == TableRef(
                database, "DEMO" if schema == "CORE" else schema, "PRICING_CONFIG"
            )
            yield LiveApps(runner, developer, Repository(runner, core, ADMIN_API), cfg, core)
    finally:
        session.close()


@pytest.fixture
def region(apps: LiveApps) -> Iterator[str]:
    key = f"AC_SMOKE_{uuid4().hex.upper()}"
    source = apps.cfg.source
    lookup = TableRef(source.database, source.schema, "REGIONS")
    assert lookup in apps.cfg.allowed_reads
    query = apps.runner.query
    try:
        query(f"INSERT INTO {lookup.sql} (CODE, NAME, RETIRED) VALUES (?, 'Smoke', FALSE)", [key])
        query(
            f"INSERT INTO {source.sql} (SKU, REGION, PRICE, CURRENCY, ACTIVE) "
            "SELECT column1, ?, column2, 'EUR', TRUE FROM VALUES ('A',100), ('B',200)",
            [key],
        )
        yield key
    finally:
        query(f"DELETE FROM {source.sql} WHERE REGION = ?", [key])
        query(f"DELETE FROM {lookup.sql} WHERE CODE = ?", [key])


@pytest.fixture
def request_id(apps: LiveApps, request: pytest.FixtureRequest) -> Iterator[str]:
    rid = str(
        apps.developer.call(
            "create_request", actor=DEV, table_id=getattr(request, "param", "PRICING_CONFIG")
        )["request_id"]
    )
    try:
        yield rid
    finally:
        if apps.developer.request(rid).status not in TERMINAL_STATUSES:
            apps.developer.call("cancel", actor=DEV, request_id=rid, reason="Smoke test teardown")
        result = apps.admin.call("cleanup", actor=ADMIN, retention_days=0, request_id=rid)
        assert result["failed"] == 0
        assert apps.developer.request(rid)["SANDBOX_DROPPED_AT"] is not None


def _prices(apps: LiveApps, region: str) -> list[Decimal]:
    rows = apps.runner.query(
        f"SELECT PRICE FROM {apps.cfg.source.sql} WHERE REGION = ? ORDER BY SKU", [region]
    )
    return [r["PRICE"] for r in rows]


def _assert_successful_dry_run(apps: LiveApps, request_id: str) -> None:
    rows = apps.runner.query(
        "SELECT LAST_ERROR IS NULL AND NL_PROMPT IS NULL AS CLEARED "
        f"FROM {apps.core}.CHANGE_REQUESTS WHERE REQUEST_ID = ?",
        [request_id],
    )
    assert rows == [{"CLEARED": True}]

    entry_point = Path(__file__).resolve().parents[1] / "streamlit/developer_app/streamlit_app.py"
    app = AppTest.from_file(str(entry_point), default_timeout=60)
    app.session_state["repository"] = apps.developer
    app.session_state["actor"] = DEV
    app.session_state["request_id"] = request_id
    app.run().switch_page("app_pages/workbench.py").run()
    assert not app.exception and not app.error
    assert all("Last error" not in warning.value for warning in app.warning)
    assert {metric.label: metric.value for metric in app.metric}["Updated"] == "2"


def test_workflow_api_boundaries(apps: LiveApps, request_id: str) -> None:
    with pytest.raises(ApiError, match="Exactly one statement"):
        apps.developer.call(
            "dry_run", actor=DEV, request_id=request_id, sql="DELETE FROM pricing_config; SELECT 1"
        )
    with pytest.raises(ApiError, match="not available"):
        apps.developer.call("review", actor=ADMIN, request_id=request_id)
    with pytest.raises(ApiError, match="not available"):
        apps.admin.call("submit", actor=DEV, request_id=request_id)
    assert apps.developer.call("cancel", actor=DEV, request_id=request_id)["status"] == "CANCELLED"


def _assert_price_preview(dev: Repository, cfg: TableConfig, diff: TableRef) -> None:
    columns = [c for c, _ in dev.columns(cfg)]
    preview = dev.diff_page(diff, ChangeKind.UPDATED, columns).sort_values("SKU (before)")
    labels = ["SKU (before)", "SKU (after)", "PRICE (before)", "PRICE (after)"]
    assert list(preview[labels].itertuples(index=False, name=None)) == [
        ("A", "A", 100, 105),
        ("B", "B", 200, 205),
    ]
    hist = dev.column_changes(diff, columns)
    assert hist["PRICE"] == 2 and hist["SKU"] == 0


def test_full_lifecycle_with_drift(apps: LiveApps, region: str, request_id: str) -> None:
    dev, admin, cfg = apps.developer, apps.admin, apps.cfg
    # The region is generated locally from a UUID. Every write is restricted to this fixture.
    change = (
        "UPDATE pricing_config SET price = price + 5 WHERE region IN "
        f"(SELECT code FROM regions WHERE NOT retired AND code = '{region}')"
    )
    dry = dev.call("dry_run", actor=DEV, request_id=request_id, sql=change)
    assert dry["counts"]["updated"] == dry["diff_counts"]["updated"] == 2
    assert _prices(apps, region) == [100, 200]

    request = dev.request(request_id)
    assert request["NL_PROMPT"] is None and request["LAST_ERROR"] is None
    _assert_successful_dry_run(apps, request_id)
    diff = request.sandbox("diff")
    _assert_price_preview(dev, cfg, diff)
    dev.call("submit", actor=DEV, request_id=request_id, sql=change)
    assert dev.request(request_id)["SUBMIT_NOTE"] is None

    assert admin.call("drift", actor=ADMIN, request_id=request_id)["classification"] == "NO_DRIFT"
    lookup = TableRef(cfg.source.database, cfg.source.schema, "REGIONS")
    apps.runner.query(f"UPDATE {lookup.sql} SET NAME = 'Changed' WHERE CODE = ?", [region])
    drift = admin.call("drift", actor=ADMIN, request_id=request_id)
    assert drift["read_table_changes"] == 1 and drift["total"] == 1

    apps.runner.query(
        f"UPDATE {cfg.source.sql} SET PRICE = PRICE + 1 WHERE REGION = ? AND SKU = 'A'", [region]
    )
    drift = admin.call("drift", actor=ADMIN, request_id=request_id)
    assert drift["classification"] == "DRIFT_OVERLAPS"
    assert drift["updated"] == drift["overlapping_keys"] == 1
    assert TableRef.parse(drift["diff_fqn"]) == diff and drift["revision"] == request.revision
    rows = admin.drift_rows(cfg, int(drift["baseline_ns"]), diff, end_ns=int(drift["end_ns"]))
    assert sorted(rows["PRICE"]) == [100, 101] and set(rows["SKU"]) == {"A"}
    with pytest.raises(ApiError, match="Re-validate"):
        admin.call("review", actor=ADMIN, request_id=request_id, decision="APPROVE", comment="ok")
    result = admin.call("revalidate", actor=ADMIN, request_id=request_id)
    assert result["counts"]["updated"] == 2 and result["same_effect"] is False
    with pytest.raises(ApiError, match="cannot review"):
        admin.call("review", actor=DEV, request_id=request_id, decision="APPROVE", comment="self")
    admin.call("review", actor=ADMIN, request_id=request_id, decision="APPROVE", comment="checked")
    apps.runner.query(
        f"UPDATE {cfg.source.sql} SET PRICE = PRICE + 1 WHERE REGION = ? AND SKU = 'B'", [region]
    )
    with pytest.raises(ApiError, match="re-validation"):
        admin.call("execute", actor=ADMIN, request_id=request_id)
    returned = admin.request(request_id)
    assert returned.status == "SUBMITTED" and returned["APPROVED_SHA256"] is None
    assert _prices(apps, region) == [101, 201]
    admin.call("revalidate", actor=ADMIN, request_id=request_id)
    revision = admin.request(request_id)["REVISION"]
    admin.call(
        "review",
        actor=ADMIN,
        request_id=request_id,
        decision="APPROVE",
        comment="Reviewed the refreshed change set",
        expected_revision=revision,
    )
    result = admin.call("execute", actor=ADMIN, request_id=request_id, expected_revision=revision)
    assert result["status"] == "EXECUTED" and result["counts"]["updated"] == 2
    assert result["query_id"] and _prices(apps, region) == [106, 206]
    assert list(admin.audit(request_id)["EVENT"]) == [
        "CREATED", "DRY_RUN_OK", "SUBMITTED", "REVALIDATED", "APPROVED", "DRIFT_BLOCKED",
        "REVALIDATED", "APPROVED", "EXECUTING", "EXECUTED",
    ]  # fmt: skip

    assert admin.call("cleanup", actor=ADMIN, retention_days=0, request_id=request_id) == {
        "dropped": 1,
        "failed": 0,
        "expired": 0,
    }
    base = str(request["SANDBOX_BASE"])
    sandbox = f"{quote_identifier(diff.database)}.{quote_identifier(diff.schema)}"
    for kind in ("TABLES", "STREAMS"):
        objects = apps.runner.query(f"SHOW {kind} IN SCHEMA {sandbox}")
        assert not any(str(row["name"]).startswith(base) for row in objects)


def test_merge_previews_and_executes_all_three_row_changes(
    apps: LiveApps, region: str, request_id: str
) -> None:
    dev, admin = apps.developer, apps.admin
    change = (
        "MERGE INTO pricing_config t USING (SELECT column1 AS sku, column2 AS op, "
        "column3 AS price FROM VALUES ('A','change',113), ('B','remove',200), ('C','add',300)) s "
        f"ON t.sku = s.sku AND t.region = '{region}' "
        "WHEN MATCHED AND s.op = 'remove' THEN DELETE "
        "WHEN MATCHED THEN UPDATE SET price = s.price "
        "WHEN NOT MATCHED AND s.op = 'add' THEN INSERT (sku, region, price, currency, active) "
        f"VALUES (s.sku, '{region}', s.price, 'EUR', TRUE)"
    )
    expected = {"inserted": 1, "updated": 1, "deleted": 1, "total": 3}
    dry = dev.call("dry_run", actor=DEV, request_id=request_id, sql=change)
    assert dry["counts"] == dry["diff_counts"] == expected
    assert _prices(apps, region) == [100, 200]
    request = dev.request(request_id)
    diff = request.sandbox("diff")
    for kind, row in [
        (ChangeKind.DELETED, ("B", 200)),
        (ChangeKind.INSERTED, ("C", 300)),
        (ChangeKind.UPDATED, ("A", "A", 100, 113)),
    ]:
        page = dev.diff_page(diff, kind, ["SKU", "PRICE"])
        assert list(page.itertuples(index=False, name=None)) == [row]

    revision = request.revision
    dev.call("submit", actor=DEV, request_id=request_id, sql=change, expected_revision=revision)
    admin.call(
        "review",
        actor=ADMIN,
        request_id=request_id,
        decision="APPROVE",
        comment="Verified the three row changes",
        expected_revision=revision,
    )
    executed = admin.call("execute", actor=ADMIN, request_id=request_id, expected_revision=revision)
    assert executed["counts"] == expected and executed["query_id"]
    assert admin.request(request_id).counts("EXEC_COUNTS") == expected
    history = apps.runner.query(
        f"SELECT QUERY_ID, QUERY_TEXT FROM TABLE({information_schema(apps.cfg.source.database)}."
        "QUERY_HISTORY_BY_SESSION(RESULT_LIMIT => 1000)) WHERE QUERY_ID IN (?, ?)",
        [dry["query_id"], executed["query_id"]],
    )
    statements = {row["QUERY_ID"]: row["QUERY_TEXT"] for row in history}
    assert dry["query_id"] != executed["query_id"]
    assert statements[dry["query_id"]].startswith(f"MERGE INTO {request.sandbox('clone').sql}")
    assert statements[executed["query_id"]].startswith(f"MERGE INTO {apps.cfg.source.sql}")
    assert apps.runner.query(
        f"SELECT SKU, PRICE FROM {apps.cfg.source.sql} WHERE REGION = ? ORDER BY SKU", [region]
    ) == [{"SKU": "A", "PRICE": 113}, {"SKU": "C", "PRICE": 300}]


@pytest.mark.skipif(
    os.environ.get("AC_ISOLATED_TESTS") != "1", reason="changes a default on an isolated fixture"
)
@pytest.mark.parametrize("request_id", ["DEFAULTS_DEMO"], indirect=True)
def test_changed_default_requires_a_new_approval(apps: LiveApps, request_id: str) -> None:
    dev, admin = apps.developer, apps.admin
    change = "INSERT INTO defaults_demo (id) VALUES (42)"
    cfg = next(t for t in dev.tables() if t.table_id == "DEFAULTS_DEMO")
    dev.call("dry_run", actor=DEV, request_id=request_id, sql=change)
    request = dev.request(request_id)
    assert list(
        dev.diff_page(request.sandbox("diff"), ChangeKind.INSERTED, ["LABEL"])["LABEL"]
    ) == ["original"]
    dev.call(
        "submit", actor=DEV, request_id=request_id, sql=change, expected_revision=request.revision
    )
    admin.call(
        "review",
        actor=ADMIN,
        request_id=request_id,
        decision="APPROVE",
        comment="Reviewed the original default",
        expected_revision=request.revision,
    )
    apps.runner.query(f"ALTER TABLE {cfg.source.sql} ALTER COLUMN LABEL DROP DEFAULT")
    with pytest.raises(ApiError, match=r"(?i)re-?validat"):
        admin.call(
            "execute", actor=ADMIN, request_id=request_id, expected_revision=request.revision
        )
    assert admin.request(request_id).status == "SUBMITTED"
    assert apps.runner.query(f"SELECT ID FROM {cfg.source.sql}") == []

    refreshed = admin.call("revalidate", actor=ADMIN, request_id=request_id)
    assert refreshed["same_effect"] is False
    revised = admin.request(request_id)
    assert revised.revision > request.revision
    assert list(
        admin.diff_page(revised.sandbox("reval_diff"), ChangeKind.INSERTED, ["LABEL"])["LABEL"]
    ) == [None]
    admin.call(
        "review",
        actor=ADMIN,
        request_id=request_id,
        decision="APPROVE",
        comment="Reviewed the NULL default",
        expected_revision=revised.revision,
    )
    admin.call("execute", actor=ADMIN, request_id=request_id, expected_revision=revised.revision)
    assert apps.runner.query(f"SELECT ID, LABEL FROM {cfg.source.sql}") == [
        {"ID": 42, "LABEL": None}
    ]


def test_sandbox_and_source_choose_the_same_key_across_sessions(
    apps: LiveApps, region: str, request_id: str
) -> None:
    # Both keys exist, so a row-count-only assertion would miss a different choice.
    predicate = (
        "MONTH(CAST('2026-11-06' AS DATE)) = 11 AND DAYOFWEEK(DATE_FROM_PARTS(2026,11,22)) = 0"
    )
    change = (
        f"UPDATE pricing_config SET price = price + 7 WHERE region = '{region}' "
        f"AND sku = IFF({predicate}, 'A', 'B')"
    )
    dev = apps.developer
    dev.call("dry_run", actor=DEV, request_id=request_id, sql=change)
    dry = dev.request(request_id)
    assert list(dev.diff_page(dry.sandbox("diff"), ChangeKind.UPDATED, ["SKU"])["SKU (after)"]) == [
        "A"
    ]
    dev.call("submit", actor=DEV, request_id=request_id, sql=change, expected_revision=dry.revision)

    session = (
        Session.builder.config("connection_name", CONNECTION).config("role", "AC_OWNER").create()
    )
    try:
        runner = SnowparkRunner(session)
        runner.query("ALTER SESSION SET DATE_INPUT_FORMAT='YYYY-DD-MM', WEEK_START=7")
        probe = f"SELECT IFF({predicate}, 'A', 'B') AS CHOSEN"
        assert runner.query(probe) == [{"CHOSEN": "B"}]
        admin = Repository(runner, apps.core, ADMIN_API)
        refreshed = admin.call("revalidate", actor=ADMIN, request_id=request_id)
        assert refreshed["same_effect"] and refreshed["query_id"] and refreshed["elapsed_ms"] >= 0
        revised = admin.request(request_id)
        admin.call(
            "review",
            actor=ADMIN,
            request_id=request_id,
            decision="APPROVE",
            comment="Verified the same key across sessions",
            expected_revision=revised.revision,
        )
        executed = admin.call(
            "execute", actor=ADMIN, request_id=request_id, expected_revision=revised.revision
        )
        assert executed["counts"]["updated"] == 1 and _prices(apps, region) == [107, 200]
        assert runner.query(probe) == [{"CHOSEN": "B"}]
    finally:
        session.close()


@pytest.mark.skipif(
    os.environ.get("AC_ISOLATED_TESTS") != "1", reason="renames columns on an isolated lookup"
)
def test_lookup_ddl_can_change_the_key_at_the_same_row_count(
    apps: LiveApps, region: str, request_id: str
) -> None:
    dev, admin = apps.developer, apps.admin
    lookup = TableRef(apps.cfg.source.database, apps.cfg.source.schema, "REGIONS")
    sql = apps.runner.query
    sql(f"ALTER TABLE {lookup.sql} ADD COLUMN ALT_NAME VARCHAR")
    sql(f"UPDATE {lookup.sql} SET NAME='A', ALT_NAME='B' WHERE CODE=?", [region])
    change = (
        f"UPDATE pricing_config SET price = price + 1 WHERE region = '{region}' "
        f"AND sku IN (SELECT name FROM regions WHERE code = '{region}')"
    )
    dev.call("dry_run", actor=DEV, request_id=request_id, sql=change)
    original = dev.request(request_id)
    assert list(
        dev.diff_page(original.sandbox("diff"), ChangeKind.UPDATED, ["SKU"])["SKU (after)"]
    ) == ["A"]
    dev.call(
        "submit", actor=DEV, request_id=request_id, sql=change, expected_revision=original.revision
    )
    admin.call(
        "review",
        actor=ADMIN,
        request_id=request_id,
        decision="APPROVE",
        comment="Reviewed SKU A",
        expected_revision=original.revision,
    )
    for old, new in [("NAME", "SWAP_NAME"), ("ALT_NAME", "NAME"), ("SWAP_NAME", "ALT_NAME")]:
        sql(f"ALTER TABLE {lookup.sql} RENAME COLUMN {old} TO {new}")
    assert sql(f"SELECT NAME FROM {lookup.sql} WHERE CODE=?", [region]) == [{"NAME": "B"}]
    with pytest.raises(ApiError, match=r"(?i)re-?validat"):
        admin.call(
            "execute", actor=ADMIN, request_id=request_id, expected_revision=original.revision
        )
    assert admin.request(request_id).status == "SUBMITTED" and _prices(apps, region) == [100, 200]
    audit = admin.audit(request_id)
    raw = audit.loc[audit["EVENT"] == "DRIFT_BLOCKED", "DETAILS"].iloc[-1]
    details = json.loads(raw) if isinstance(raw, str) else raw
    assert details["reason"] == "LOOKUP_DDL_CHANGED" and details["table_fqn"] == str(lookup)
    assert details["last_ddl_ns"] > details["baseline_ns"]
    rerun = admin.call("revalidate", actor=ADMIN, request_id=request_id)
    assert rerun["same_effect"] is False and rerun["counts"]["updated"] == 1
    latest = admin.request(request_id)
    observed = admin.diff_page(latest.sandbox("reval_diff"), ChangeKind.UPDATED, ["SKU"])
    assert list(observed["SKU (after)"]) == ["B"]
