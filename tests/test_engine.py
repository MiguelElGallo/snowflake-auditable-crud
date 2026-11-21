"""Workflow tests for the engine with an in-memory store and a scripted SQL runner."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any
from unittest.mock import Mock

import pytest
from fakes import CONFIG, SOURCE, UPDATE_SQL, FakeSnowflake, MemoryStore

from auditable_crud.diff import DiffCounts
from auditable_crud.engine import (
    ADMIN_OPS,
    DEVELOPER_OPS,
    Engine,
    dispatch,
    sha256,
    within_tolerance,
)
from auditable_crud.runner import DML_CONTEXT_VERSION, Row
from auditable_crud.state import WorkflowError


@pytest.fixture
def sf() -> FakeSnowflake:
    return FakeSnowflake()


@pytest.fixture
def store(sf: FakeSnowflake) -> MemoryStore:
    return MemoryStore(sf)


@pytest.fixture
def engine(sf: FakeSnowflake, store: MemoryStore) -> Engine:
    ids = iter(["11111111-aaaa", "22222222-bbbb"])
    return Engine(sf, store, new_id=lambda: next(ids))


def _submitted(engine: Engine) -> str:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    engine.dry_run(rid, "dev", UPDATE_SQL)
    engine.submit(rid, "dev")
    return str(rid)


def test_full_lifecycle_runs_dml_on_clone_then_on_source(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    request = store.get(rid)
    assert request["STATUS"] == "SUBMITTED"
    assert request["SANDBOX_BASE"].startswith("PRICING_CONFIG__CR_")
    assert request["DRY_RUN_COUNTS"]["updated"] == 3

    engine.review(rid, "admin", "approve", "Looks right")
    before_execution = len(sf.log)
    result = engine.execute(rid, "admin")

    dry, real = sf.dml_statements()
    assert dry.startswith('UPDATE "APP"."SANDBOX"."PRICING_CONFIG__CR_') and SOURCE not in dry
    assert real.startswith(f"UPDATE {SOURCE} SET")
    tx = [s for s in sf.log[before_execution:] if s in {"BEGIN TRANSACTION", "COMMIT", "ROLLBACK"}]
    assert tx == ["BEGIN TRANSACTION", "COMMIT"]
    assert result["counts"]["updated"] == 3
    assert store.get(rid)["STATUS"] == "EXECUTED"
    assert [e for _, e, _ in store.events] == [
        "CREATED", "DRY_RUN_OK", "SUBMITTED", "APPROVED", "EXECUTING", "EXECUTED",
    ]  # fmt: skip


def test_stream_is_created_after_clone_and_before_dml(engine: Engine, sf: FakeSnowflake) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    sf.log.clear()
    engine.dry_run(rid, "dev", UPDATE_SQL)
    kinds = [s.split(" ")[0:4] for s in sf.log if s.startswith(("CREATE", "UPDATE"))]
    assert [k[3] if k[0] == "CREATE" else "DML" for k in kinds] == [
        "TABLE", "STREAM", "DML", "TABLE",
    ]  # fmt: skip


def test_clone_comment_identifies_the_request(engine: Engine, sf: FakeSnowflake) -> None:
    rid = engine.create_request("PRICING_CONFIG", "o'brien")["request_id"]
    comment = next(s for s in sf.log if s.startswith("ALTER TABLE"))
    assert '"APP"."SANDBOX"."PRICING_CONFIG__CR_' in comment
    assert f"request {rid} by o''brien on APP.DEMO.PRICING_CONFIG'" in comment


def test_requester_cannot_approve_or_execute_own_change(engine: Engine, store: MemoryStore) -> None:
    rid = _submitted(engine)
    with pytest.raises(WorkflowError, match="cannot review"):
        engine.review(rid, "DEV", "approve", "self")
    engine.review(rid, "admin", "approve", "ok")
    with pytest.raises(WorkflowError, match="cannot execute"):
        engine.execute(rid, "dev")
    assert store.get(rid)["STATUS"] == "APPROVED"


def test_submit_requires_rows_within_limit(engine: Engine, sf: FakeSnowflake) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    sf.dml_result = {"number of rows updated": 0}
    engine.dry_run(rid, "dev", UPDATE_SQL)
    with pytest.raises(WorkflowError, match="no rows"):
        engine.submit(rid, "dev")
    sf.dml_result = {"number of rows updated": 101}
    engine.dry_run(rid, "dev", UPDATE_SQL)
    with pytest.raises(WorkflowError, match="above the limit of 100"):
        engine.submit(rid, "dev")


def test_approval_blocked_by_drift_until_revalidated(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    sf.drift = {"INSERTED": 0, "UPDATED": 2, "DELETED": 0}
    sf.overlap = 1
    assert engine.drift(rid)["classification"] == "DRIFT_OVERLAPS"
    with pytest.raises(WorkflowError, match="Re-validate"):
        engine.review(rid, "admin", "approve", "ok")

    reval = engine.revalidate(rid, "admin")
    assert reval["same_effect"] is True
    baseline = store.get(rid)["REVAL_SNAPSHOT_NS"]
    assert baseline > store.get(rid)["SNAPSHOT_NS"]
    sf.drift = {"INSERTED": 0, "UPDATED": 0, "DELETED": 0}
    engine.review(rid, "admin", "approve", "ok after re-validation")
    assert store.get(rid)["STATUS"] == "APPROVED"


def test_drift_after_approval_sends_request_back(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "approve", "ok")
    sf.drift = {"INSERTED": 1, "UPDATED": 0, "DELETED": 0}
    with pytest.raises(WorkflowError, match="back to SUBMITTED"):
        engine.execute(rid, "admin")
    assert store.get(rid)["STATUS"] == "SUBMITTED"
    assert len(sf.dml_statements()) == 1  # only the dry run; source untouched


def test_row_count_mismatch_rolls_back(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "approve", "ok", tolerance_pct=10)
    audit = Mock(wraps=store.audit)
    monkeypatch.setattr(store, "audit", audit)
    sf.dml_result = {"number of rows updated": 5}
    original = sf.query

    def with_source_query_id(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "LAST_QUERY_ID()" in sql:
            return [{"QID": "rolled-back-source-query"}]
        return original(sql, params)

    monkeypatch.setattr(sf, "query", with_source_query_id)
    with pytest.raises(WorkflowError, match="Rolled back"):
        engine.execute(rid, "admin")
    assert sf.log[-3:] == ["ROLLBACK", "BEGIN TRANSACTION", "COMMIT"]
    result = store.get(rid)
    assert result["STATUS"] == "FAILED"
    details = audit.call_args.args[3]
    assert details["query_id"] == result["EXEC_QUERY_ID"] == "rolled-back-source-query"
    assert details["elapsed_ms"] == result["EXEC_ELAPSED_MS"]
    assert details["counts"] == result["EXEC_COUNTS"] == DiffCounts(updated=5).as_dict()


JOINED_UPDATE = (
    "UPDATE pricing_config t SET price = src.price FROM pricing_config src WHERE t.sku = src.sku"
)


def test_ambiguous_join_fails_the_dry_run_and_a_one_to_one_retry_can_execute(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    sf.dml_result = {"number of rows updated": 3, "number of multi-joined rows updated": 1}
    with pytest.raises(WorkflowError, match="multiple source rows for 1 target"):
        engine.dry_run(rid, "dev", JOINED_UPDATE)
    assert store.get(rid)["STATUS"] == "DRAFT"
    assert store.events[-1][1] == "DRY_RUN_FAILED"
    assert all(SOURCE not in dml for dml in sf.dml_statements())

    sf.dml_result["number of multi-joined rows updated"] = 0
    counts = {"inserted": 0, "updated": 3, "deleted": 0, "total": 3}
    assert engine.dry_run(rid, "dev", JOINED_UPDATE)["counts"] == counts
    engine.submit(rid, "dev")
    engine.review(rid, "admin", "approve", "one source per target")
    assert engine.execute(rid, "admin")["counts"] == counts
    assert store.get(rid)["STATUS"] == "EXECUTED"


def test_ambiguous_source_update_rolls_back_even_when_approved_counts_match(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    sf.dml_result = {"NUMBER OF ROWS UPDATED": 3, "NUMBER OF MULTI-JOINED ROWS UPDATED": 0}
    engine.dry_run(rid, "dev", JOINED_UPDATE)
    engine.submit(rid, "dev")
    engine.review(rid, "admin", "approve", "ok", tolerance_pct=100)

    sf.dml_result["NUMBER OF MULTI-JOINED ROWS UPDATED"] = 2
    sf.log.clear()
    with pytest.raises(WorkflowError, match="multiple source rows for 2 target"):
        engine.execute(rid, "admin")
    assert sf.dml_statements()[0].startswith(f"UPDATE {SOURCE}")
    tx = [s for s in sf.log if s in {"BEGIN TRANSACTION", "COMMIT", "ROLLBACK"}]
    assert tx == ["BEGIN TRANSACTION", "ROLLBACK", "BEGIN TRANSACTION", "COMMIT"]
    assert store.get(rid)["STATUS"] == "FAILED"
    assert store.get(rid)["EXEC_QUERY_ID"] == "01-query"
    assert store.get(rid)["EXEC_COUNTS"]["updated"] == 3
    assert store.events[-1][1] == "EXECUTION_ROLLED_BACK"


def test_source_error_records_its_own_query_id(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "APPROVE", "reviewed")
    original = sf.query

    class SourceFailureError(RuntimeError):
        sfqid = "failed-source-statement"

    def fail_dml(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if sql.startswith(f"UPDATE {SOURCE}"):
            raise SourceFailureError("conversion failed")
        return original(sql, params)

    monkeypatch.setattr(sf, "query", fail_dml)
    audit = Mock(wraps=store.audit)
    monkeypatch.setattr(store, "audit", audit)
    with pytest.raises(SourceFailureError):
        engine.execute(rid, "admin")
    result = store.get(rid)
    assert result["STATUS"] == "FAILED" and result["EXEC_COUNTS"] is None
    assert (
        result["EXEC_QUERY_ID"] == audit.call_args.args[3]["query_id"] == SourceFailureError.sfqid
    )
    assert result["EXEC_ELAPSED_MS"] == audit.call_args.args[3]["elapsed_ms"]


def test_database_error_during_execution_rolls_back(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "approve", "ok")
    sf.fail_dml_on = SOURCE
    with pytest.raises(RuntimeError):
        engine.execute(rid, "admin")
    assert sf.log[-3:] == ["ROLLBACK", "BEGIN TRANSACTION", "COMMIT"]
    assert store.get(rid)["STATUS"] == "FAILED"


def test_tampered_sql_is_refused(engine: Engine, store: MemoryStore) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "approve", "ok")
    store.requests[rid]["CANONICAL_SQL"] = f"DELETE FROM {SOURCE}"
    with pytest.raises(WorkflowError, match="no longer matches"):
        engine.execute(rid, "admin")


def test_concurrent_status_change_is_detected(
    engine: Engine, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _submitted(engine)
    stale = store.get(rid)  # admin B loaded the page while it was SUBMITTED ...
    engine.review(rid, "admin_a", "reject", "no")  # ... then admin A rejected it
    monkeypatch.setattr(store, "get", lambda _rid: dict(stale))
    with pytest.raises(WorkflowError, match="changed by someone else"):
        engine.review(rid, "admin_b", "approve", "yes")
    assert store.requests[rid]["STATUS"] == "REJECTED"


def test_cleanup_drops_all_sandbox_objects(engine: Engine, sf: FakeSnowflake) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "reject", "no")
    assert engine.cleanup(7, "task")["dropped"] == 1
    drops = [s for s in sf.log if s.startswith("DROP ")]
    assert len(drops) == 9 and all("IF EXISTS" in d and '"SANDBOX"' in d for d in drops)


def test_failed_dry_run_records_error_and_keeps_draft(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    sf.fail_dml_on = '"SANDBOX"'
    with pytest.raises(RuntimeError, match="not recognized"):
        engine.dry_run(rid, "dev", UPDATE_SQL)
    request = store.get(rid)
    assert request["STATUS"] == "DRAFT"
    assert "not recognized" in request["LAST_ERROR"]
    assert store.events[-1][1] == "DRY_RUN_FAILED"


def test_only_the_requester_can_cancel(engine: Engine, store: MemoryStore) -> None:
    rid = _submitted(engine)
    with pytest.raises(WorkflowError, match="Only dev"):
        engine.cancel(rid, "admin")
    engine.cancel(rid, "dev", "changed my mind")
    assert store.get(rid)["STATUS"] == "CANCELLED"
    with pytest.raises(WorkflowError, match="cannot move"):
        engine.submit(rid, "dev")  # a cancelled request stays cancelled


def test_submit_refuses_sql_edited_after_dry_run(engine: Engine, store: MemoryStore) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    engine.dry_run(rid, "dev", UPDATE_SQL)
    with pytest.raises(WorkflowError, match="edited after the last dry run"):
        engine.submit(rid, "dev", sql=f"DELETE FROM {SOURCE}")
    assert store.get(rid)["STATUS"] == "DRY_RUN_OK"
    engine.submit(rid, "dev", sql=f"  {UPDATE_SQL}\n")  # whitespace is not an edit
    assert store.get(rid)["STATUS"] == "SUBMITTED"


def test_approval_enforces_row_limit_on_revalidated_counts(
    engine: Engine, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    store.requests[rid]["REVAL_COUNTS"] = {"inserted": 0, "updated": 500, "deleted": 0}
    with pytest.raises(WorkflowError, match="exceed the limit of 100"):
        engine.review(rid, "admin", "approve", "ok")
    assert store.get(rid)["STATUS"] == "SUBMITTED"


def test_execution_above_row_limit_rolls_back_even_within_tolerance(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    sf.dml_result = {"number of rows updated": 60}
    engine.dry_run(rid, "dev", UPDATE_SQL)
    engine.submit(rid, "dev")
    engine.review(rid, "admin", "approve", "ok", tolerance_pct=100)
    sf.dml_result = {"number of rows updated": 110}  # within 100% tolerance, above limit 100
    with pytest.raises(WorkflowError, match="above the limit of 100"):
        engine.execute(rid, "admin")
    assert sf.log[-3:] == ["ROLLBACK", "BEGIN TRANSACTION", "COMMIT"]
    assert store.get(rid)["STATUS"] == "FAILED"
    assert store.events[-1][1] == "EXECUTION_ROLLED_BACK"


def test_executed_status_and_audit_are_written_before_commit(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "approve", "ok")
    audit = store.audit

    def logged_audit(request_id: str, event: str, actor: str, details: object) -> None:
        sf.log.append(f"AUDIT {event}")
        audit(request_id, event, actor, details)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "audit", logged_audit)
    sf.log.clear()
    engine.execute(rid, "admin")
    assert sf.log.index("AUDIT EXECUTED") < sf.log.index("COMMIT")
    assert store.get(rid)["STATUS"] == "EXECUTED"


def test_failed_rollback_does_not_hide_the_original_error(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "approve", "ok")
    sf.fail_dml_on = SOURCE
    sf.fail_on = "ROLLBACK"
    sf.log.clear()
    with pytest.raises(RuntimeError, match="not recognized"):
        engine.execute(rid, "admin")
    assert sf.log[-1] == "ROLLBACK"
    assert "COMMIT" not in sf.log  # never commit an unacknowledged rollback


def test_cleanup_covers_failed_and_cancelled_and_runs_once(
    engine: Engine, store: MemoryStore
) -> None:
    cancelled = _submitted(engine)
    engine.cancel(cancelled, "dev")
    failed = _submitted(engine)
    store.requests[failed]["STATUS"] = "FAILED"
    assert engine.cleanup(7, "task")["dropped"] == 2
    assert engine.cleanup(7, "task")["dropped"] == 0


def test_tolerance_math() -> None:
    assert within_tolerance(DiffCounts(0, 100, 0), DiffCounts(0, 105, 0), 5)
    assert not within_tolerance(DiffCounts(0, 100, 0), DiffCounts(0, 106, 0), 5)
    assert not within_tolerance(DiffCounts(0, 0, 0), DiffCounts(1, 0, 0), 50)
    assert not within_tolerance(DiffCounts(0, 1, 0), DiffCounts(0, 2, 0), 1)


def test_dispatch_passes_zero_retention_through(
    sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``retention_days=0`` must not fall back to the default (found in Snowflake smoke test)."""
    seen: list[int] = []

    def fake_cleanup(
        _self: Engine, days: int, _actor: str, _rid: str | None, _idle: int
    ) -> dict[str, int]:
        seen.append(days)
        return {"dropped": 0}

    monkeypatch.setattr(Engine, "cleanup", fake_cleanup)
    dispatch(sf, '"APP"."CORE"', "cleanup", {"actor": "task", "retention_days": 0})
    dispatch(sf, '"APP"."CORE"', "cleanup", {"actor": "task"})
    assert seen == [0, 7]


def test_dispatch_reports_errors_without_raising(sf: FakeSnowflake) -> None:
    assert dispatch(sf, '"APP"."CORE"', "nope", {"actor": "x"}) == {
        "ok": False,
        "error": "Unknown operation 'nope'.",
        "error_type": "WorkflowError",
    }
    missing_actor = dispatch(sf, '"APP"."CORE"', "drift", '{"request_id": "r"}')
    assert missing_actor["ok"] is False and "actor is required" in missing_actor["error"]


def test_overlapping_runs_cannot_publish_or_replace_each_others_diff(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    query = sf.query
    first = True

    def interleave(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        nonlocal first
        if first and sql.startswith("UPDATE "):
            first = False
            engine.dry_run(rid, "dev", "UPDATE pricing_config SET price = 9 WHERE sku = 'B'")
        return query(sql, params)

    monkeypatch.setattr(sf, "query", interleave)
    with pytest.raises(WorkflowError, match="changed by someone else"):
        engine.dry_run(rid, "dev", UPDATE_SQL)
    current = store.get(rid)
    assert current["RUN_REVISION"] == current["DRY_SANDBOX_REVISION"] == 2
    assert "price = 9" in current["SQL_TEXT"]
    assert any("_R1_DIFF" in sql for sql in sf.log if sql.startswith("DROP "))
    assert not any("_R2_" in sql for sql in sf.log if sql.startswith("DROP "))
    assert any("_R2_DIFF" in sql for sql in sf.log if sql.startswith("CREATE "))
    engine.submit(rid, "dev")


def test_failed_rerun_invalidates_previous_approval_evidence_and_can_retry(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    engine.revalidate(rid, "admin")
    sf.fail_dml_on = '"SANDBOX"'
    with pytest.raises(RuntimeError):
        engine.revalidate(rid, "admin")
    assert store.get(rid)["REVAL_SANDBOX_REVISION"] == 2
    with pytest.raises(WorkflowError, match="did not finish"):
        engine.review(rid, "admin", "APPROVE", "ok")
    sf.fail_dml_on = None
    engine.revalidate(rid, "admin")
    engine.review(rid, "admin", "APPROVE", "ok")


def test_stale_displayed_revision_and_approval_cycles(
    engine: Engine, store: MemoryStore, sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    engine.dry_run(rid, "dev", UPDATE_SQL)
    with pytest.raises(WorkflowError, match="since it was displayed"):
        engine.submit(rid, "dev", expected_revision=0)
    engine.submit(rid, "dev", expected_revision=1)
    engine.revalidate(rid, "admin")
    with pytest.raises(WorkflowError, match="since it was displayed"):
        engine.review(rid, "admin", "APPROVE", "old diff", expected_revision=1)
    engine.review(rid, "admin", "APPROVE", "current diff", expected_revision=2)
    engine.send_back(rid, "admin", "reconsider", expected_revision=2)
    engine.review(rid, "admin", "APPROVE", "updated tolerance", 10, expected_revision=3)
    with pytest.raises(WorkflowError, match="since it was displayed"):
        engine.execute(rid, "admin", expected_revision=2)
    monkeypatch.setattr("auditable_crud.engine.SqlRequestStore", lambda *_: store)
    stale = dispatch(
        sf,
        '"APP"."CORE"',
        "send_back",
        {"request_id": rid, "actor": "admin", "comment": "old screen", "expected_revision": 2},
        ADMIN_OPS,
    )
    assert not stale["ok"] and "since it was displayed" in stale["error"]
    assert store.get(rid)["STATUS"] == "APPROVED"


def test_failed_audit_rolls_back_the_source_and_completed_status(
    engine: Engine, store: MemoryStore, sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "APPROVE", "ok")
    audit = store.audit
    original = sf.query

    def fail_execution_audit(r: str, event: str, actor: str, details: Mapping[str, Any]) -> None:
        if event == "EXECUTED":
            assert database.execute("SELECT SUM(price) FROM source").fetchone() == (63,)
            raise RuntimeError("audit unavailable")
        audit(r, event, actor, details)

    monkeypatch.setattr(store, "audit", fail_execution_audit)
    # The SQL recorder alone cannot tell if a source write survived a rollback.
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as database:
        database.execute("CREATE TABLE source (price INTEGER)")
        database.execute("INSERT INTO source VALUES (10), (20), (30)")

        def run_source(sql: str, params: Sequence[Any] = ()) -> list[Row]:
            if sql in {"BEGIN TRANSACTION", "COMMIT", "ROLLBACK"}:
                database.execute(sql)
            if sql.startswith(f"UPDATE {SOURCE}"):
                count = database.execute("UPDATE source SET price = price + 1").rowcount
                return [{"number of rows updated": count}]
            return original(sql, params)

        monkeypatch.setattr(sf, "query", run_source)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            engine.execute(rid, "admin")
        assert database.execute("SELECT SUM(price) FROM source").fetchone() == (60,)
    assert sf.log[-3:] == ["ROLLBACK", "BEGIN TRANSACTION", "COMMIT"]
    assert store.get(rid)["STATUS"] == "FAILED"
    assert [event for _, event, _ in store.events][-2:] == ["APPROVED", "EXECUTION_FAILED"]


def test_concurrent_source_change_during_execution_rolls_back_even_with_matching_counts(
    engine: Engine, store: MemoryStore, sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "APPROVE", "ok")
    query = sf.query

    def concurrent_change(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        rows = query(sql, params)
        if sql.startswith(f"UPDATE {SOURCE}"):
            sf.drift["UPDATED"] = 1
        return rows

    monkeypatch.setattr(sf, "query", concurrent_change)
    with pytest.raises(WorkflowError, match="back to SUBMITTED"):
        engine.execute(rid, "admin")
    assert sf.log[-3:] == ["ROLLBACK", "BEGIN TRANSACTION", "COMMIT"]
    assert store.get(rid)["STATUS"] == "SUBMITTED"
    assert store.events[-1][1] == "DRIFT_BLOCKED"


def test_config_changes_cannot_redirect_requests_or_sandboxes(
    engine: Engine, store: MemoryStore, sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    monkeypatch.setattr(store, "load_config", lambda _: {**CONFIG, "CLONE_SCHEMA": "NEW_STAGE"})
    engine.dry_run(rid, "dev", UPDATE_SQL)
    assert "NEW_STAGE" not in " ".join(sf.log)
    monkeypatch.setattr(store, "load_config", lambda _: {**CONFIG, "SOURCE_TABLE": "OTHER"})
    with pytest.raises(WorkflowError, match="different source"):
        engine.submit(rid, "dev")


def _submit_with_lookup(engine: Engine, store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(
        store,
        "load_config",
        lambda _: {**CONFIG, "ALLOWED_READ_TABLES": "REF.DATA.REGIONS,REF.DATA.UNUSED"},
    )
    rid = str(engine.create_request("PRICING_CONFIG", "dev")["request_id"])
    engine.dry_run(
        rid,
        "dev",
        "UPDATE pricing_config SET price = price + 1 "
        "WHERE region IN (SELECT code FROM ref.data.regions)",
    )
    engine.submit(rid, "dev")
    return rid


@pytest.mark.parametrize(
    ("object_name", "reason"),
    [("PRICING_CONFIG", "TARGET_DDL_CHANGED"), ("REGIONS", "LOOKUP_DDL_CHANGED")],
)
def test_approval_audits_definition_drift_even_with_unchanged_columns_and_row_counts(
    engine: Engine,
    store: MemoryStore,
    sf: FakeSnowflake,
    monkeypatch: pytest.MonkeyPatch,
    *,
    object_name: str,
    reason: str,
) -> None:
    rid = _submit_with_lookup(engine, store, monkeypatch)
    baseline = store.get(rid)["SNAPSHOT_NS"]
    definition_reads = []
    query = sf.query

    def metadata(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "COALESCE(LAST_DDL, CREATED)" in sql:
            definition_reads.append((sql, tuple(params)))
            return [{"DDL_NS": baseline + 1 if params[1] == object_name else 1}]
        return query(sql, params)

    monkeypatch.setattr(sf, "query", metadata)
    audit = Mock(wraps=store.audit)
    monkeypatch.setattr(store, "audit", audit)
    with pytest.raises(WorkflowError, match=r"definition.*Re-validate"):
        engine.review(rid, "admin", "APPROVE", "reviewed")
    assert store.get(rid)["STATUS"] == "SUBMITTED"
    event, details = audit.call_args.args[1], audit.call_args.args[3]
    assert event == "APPROVAL_BLOCKED" and details["reason"] == reason
    assert details["baseline_ns"] == baseline and details["last_ddl_ns"] == baseline + 1
    assert all(args[1] != "UNUSED" for _, args in definition_reads)
    if object_name == "REGIONS":
        assert any(
            '"REF".INFORMATION_SCHEMA.TABLES' in sql and args == ("DATA", "REGIONS")
            for sql, args in definition_reads
        )
    engine.revalidate(rid, "admin")
    engine.review(rid, "admin", "APPROVE", "reviewed after the definition change")


@pytest.mark.parametrize("after_dml", [False, True])
def test_lookup_definition_change_rolls_back_source_and_allows_a_fresh_review(
    engine: Engine,
    store: MemoryStore,
    sf: FakeSnowflake,
    monkeypatch: pytest.MonkeyPatch,
    after_dml: bool,
) -> None:
    rid = _submit_with_lookup(engine, store, monkeypatch)
    engine.review(rid, "admin", "APPROVE", "original")
    baseline = store.get(rid)["SNAPSHOT_NS"]
    changed = not after_dml
    query = sf.query
    audit = Mock(wraps=store.audit)
    monkeypatch.setattr(store, "audit", audit)
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as source:
        source.execute("CREATE TABLE prices (amount INTEGER)")
        source.executemany("INSERT INTO prices VALUES (?)", [(10,), (20,), (30,)])

        def concurrent_ddl(sql: str, params: Sequence[Any] = ()) -> list[Row]:
            nonlocal changed
            if "COALESCE(LAST_DDL, CREATED)" in sql and params[1] == "REGIONS":
                return [{"DDL_NS": baseline + 1 if changed else 1}]
            if sql in {"BEGIN TRANSACTION", "ROLLBACK", "COMMIT"}:
                source.execute(sql)
            if sql.startswith(f"UPDATE {SOURCE}"):
                source.execute("UPDATE prices SET amount = amount + 1")
                changed = True
            return query(sql, params)

        monkeypatch.setattr(sf, "query", concurrent_ddl)
        with pytest.raises(WorkflowError, match=r"definition.*back to SUBMITTED"):
            engine.execute(rid, "admin")
        assert source.execute("SELECT amount FROM prices ORDER BY amount").fetchall() == [
            (10,),
            (20,),
            (30,),
        ]
        details = audit.call_args.args[3]
        assert audit.call_args.args[1] == "DRIFT_BLOCKED"
        assert details["reason"] == "LOOKUP_DDL_CHANGED"
        assert bool(details.get("query_id")) == after_dml
        assert store.get(rid)["STATUS"] == "SUBMITTED"
        engine.revalidate(rid, "admin")
        engine.review(rid, "admin", "APPROVE", "fresh lookup")
        assert engine.execute(rid, "admin")["status"] == "EXECUTED"
        assert source.execute("SELECT amount FROM prices ORDER BY amount").fetchall() == [
            (11,),
            (21,),
            (31,),
        ]


@pytest.mark.parametrize("metadata_rows", [[], [{"DDL_NS": None}]])
def test_lookup_with_unavailable_definition_timestamp_cannot_be_approved(
    engine: Engine,
    store: MemoryStore,
    sf: FakeSnowflake,
    monkeypatch: pytest.MonkeyPatch,
    metadata_rows: list[Row],
) -> None:
    rid = _submit_with_lookup(engine, store, monkeypatch)
    query = sf.query

    def missing_definition(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "COALESCE(LAST_DDL, CREATED)" in sql and params[1] == "REGIONS":
            return metadata_rows
        return query(sql, params)

    monkeypatch.setattr(sf, "query", missing_definition)
    audit = Mock(wraps=store.audit)
    monkeypatch.setattr(store, "audit", audit)
    with pytest.raises(WorkflowError, match=r"timestamp.*unavailable"):
        engine.review(rid, "admin", "APPROVE", "missing lookup metadata")
    assert audit.call_args.args[3]["reason"] == "LOOKUP_DDL_UNAVAILABLE"
    assert store.get(rid)["STATUS"] == "SUBMITTED"


def test_lookup_drift_is_checked_for_approval(
    engine: Engine, store: MemoryStore, sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        store, "load_config", lambda _: {**CONFIG, "ALLOWED_READ_TABLES": "APP.DEMO.REGIONS"}
    )
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    engine.dry_run(
        rid, "dev", "UPDATE pricing_config SET price = 3 WHERE region IN (SELECT code FROM regions)"
    )
    baseline = store.get(rid)["SNAPSHOT_NS"]
    dml = next(q for q in sf.log if q.startswith('UPDATE "APP"."SANDBOX"'))
    assert f'"REGIONS" AT (TIMESTAMP => TO_TIMESTAMP_LTZ({baseline}, 9))' in dml
    engine.submit(rid, "dev")
    query = sf.query
    history_queries: list[str] = []

    def drift_in_lookup(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if " CHANGES(" in sql:
            history_queries.append(sql)
        if '"REGIONS" CHANGES(' in sql:
            return [{"INSERTED": 1, "UPDATED": 0, "DELETED": 0}]
        return query(sql, params)

    monkeypatch.setattr(sf, "query", drift_in_lookup)
    report = engine.drift(rid)
    assert report["read_table_changes"] == report["total"] == 1
    assert report["revision"] == store.get(rid)["REVISION"]
    assert report["source_fqn"] == "APP.DEMO.PRICING_CONFIG" and report["key_columns"] == ["SKU"]
    assert report["diff_fqn"].startswith("APP.SANDBOX.") and report["diff_fqn"].endswith("_DIFF")
    end = f"END(TIMESTAMP => TO_TIMESTAMP_LTZ({report['end_ns']}, 9))"
    assert report["end_ns"] > report["baseline_ns"] == baseline
    assert len(history_queries) == 3 and all(end in sql for sql in history_queries)
    with pytest.raises(WorkflowError, match="Re-validate"):
        engine.review(rid, "admin", "APPROVE", "ok")


def test_cleanup_only_expires_idle_drafts_and_can_target_one_request(
    engine: Engine,
    store: MemoryStore,
) -> None:
    idle = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    submitted = _submitted(engine)
    store.requests[idle]["IDLE_DAYS"] = store.requests[submitted]["IDLE_DAYS"] = 8
    assert engine.cleanup(0, "task", request_id=idle)["dropped"] == 0
    assert engine.cleanup(0, "task", idle_draft_days=7) == {"expired": 1, "dropped": 1, "failed": 0}
    assert store.get(submitted)["STATUS"] == "SUBMITTED"
    assert [e for r, e, _ in store.events if r == idle] == [
        "CREATED",
        "DRAFT_EXPIRED",
        "SANDBOX_DROPPED",
    ]
    engine.cancel(submitted, "dev")
    assert engine.cleanup(0, "task", request_id=idle)["dropped"] == 0
    assert not store.get(submitted).get("SANDBOX_DROPPED_AT")


def test_roles_cannot_cross_procedure_boundaries(sf: FakeSnowflake) -> None:
    for op, allowed in [
        ("review", DEVELOPER_OPS),
        ("execute", DEVELOPER_OPS),
        ("submit", ADMIN_OPS),
    ]:
        response = dispatch(sf, '"APP"."CORE"', op, {"actor": "dev"}, allowed)
        assert response["ok"] is False and "not available" in response["error"]
    assert sf.log == []


@pytest.mark.parametrize(
    "args", [{"request_id": ""}, {"request_id": None}, {"retention_days": 0.5}]
)
def test_invalid_cleanup_scope_cannot_select_unrelated_requests(
    sf: FakeSnowflake, args: Row
) -> None:
    response = dispatch(sf, '"APP"."CORE"', "cleanup", {"actor": "task", **args}, ADMIN_OPS)
    assert response["ok"] is False
    assert sf.log == []


def test_early_retention_check_creates_no_sandbox(
    engine: Engine, sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = sf.query

    def zero_retention(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if sql.startswith("SELECT RETENTION_TIME"):
            return [{"RETENTION_TIME": 0}]
        return query(sql, params)

    monkeypatch.setattr(sf, "query", zero_retention)
    with pytest.raises(WorkflowError, match="Time Travel retention"):
        engine.create_request("PRICING_CONFIG", "dev")
    assert not any(sql.startswith("CREATE ") for sql in sf.log)


def test_clone_default_schema_is_checked_before_sandbox_dml(
    engine: Engine, sf: FakeSnowflake, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    query = sf.query

    def clone_metadata(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "INFORMATION_SCHEMA.COLUMNS" in sql and params[0] == "SANDBOX":
            return [
                {"COLUMN_NAME": "SKU", "COLUMN_DEFAULT": "CAST(APP.DEMO.IDS.NEXTVAL AS NUMBER)"}
            ]
        return query(sql, params)

    monkeypatch.setattr(sf, "query", clone_metadata)
    with pytest.raises(WorkflowError, match="sequence default"):
        engine.dry_run(rid, "dev", "INSERT INTO pricing_config (price) VALUES (1)")
    assert sf.dml_statements() == []


def test_interrupted_sandbox_is_eligible_after_maximum_snowflake_query_runtime(
    engine: Engine,
    store: MemoryStore,
) -> None:
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    store.requests[rid].update(REVISION=1, RUN_REVISION=0, LAST_ERROR=None, IDLE_DAYS=2)
    assert engine.cleanup(0, "task", idle_draft_days=1)["expired"] == 0
    store.requests[rid]["IDLE_DAYS"] = 8
    assert engine.cleanup(0, "task", idle_draft_days=1)["expired"] == 1
    assert store.get(rid)["STATUS"] == "CANCELLED"


def test_unknown_dml_result_cannot_pass_a_tolerant_approval(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "APPROVE", "ok", tolerance_pct=100)
    sf.dml_result = {"status": "unrecognized"}
    with pytest.raises(ValueError, match="DML row counts"):
        engine.execute(rid, "admin")
    assert store.get(rid)["STATUS"] == "FAILED"
    assert sf.log[-3:] == ["ROLLBACK", "BEGIN TRANSACTION", "COMMIT"]


@pytest.mark.parametrize(
    "change",
    [
        {"COLUMN_DEFAULT": None},
        {"DATA_TYPE": "FLOAT"},
        {"IS_NULLABLE": "NO"},
        {"NUMERIC_PRECISION": 12},
        {"NUMERIC_SCALE": 4},
        {"CHARACTER_MAXIMUM_LENGTH": 20},
        {"REVERSE_COLUMNS": True},
    ],
)
def test_approval_compares_the_latest_clone_schema(
    engine: Engine,
    sf: FakeSnowflake,
    store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    change: Row,
) -> None:
    rid = _submitted(engine)
    before: list[Row] = [
        {"COLUMN_NAME": "SKU"},
        {
            "COLUMN_NAME": "PRICE",
            "DATA_TYPE": "NUMBER",
            "NUMERIC_PRECISION": 9,
            "NUMERIC_SCALE": 2,
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": "10",
        },
    ]
    after = before[::-1] if "REVERSE_COLUMNS" in change else [before[0], {**before[1], **change}]
    original = sf.query

    def query(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return before if str(params[1]).endswith("_R1") else after
        return original(sql, params)

    monkeypatch.setattr(sf, "query", query)
    with pytest.raises(WorkflowError, match=r"schema.*Re-validate"):
        engine.review(rid, "admin", "APPROVE", "stale schema")
    audit = Mock(wraps=store.audit)
    monkeypatch.setattr(store, "audit", audit)
    result = engine.revalidate(rid, "admin")
    assert audit.call_args.args == (rid, "REVALIDATED", "admin", result)
    assert result["revision"] == 2 and result["query_id"] == "01-query"
    assert result["elapsed_ms"] >= 0
    engine.review(rid, "admin", "APPROVE", "updated schema")
    assert engine.execute(rid, "admin")["status"] == "EXECUTED"


@pytest.mark.parametrize("after_dml", [False, True])
def test_schema_change_sends_old_approval_back(
    engine: Engine,
    sf: FakeSnowflake,
    store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    after_dml: bool,
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "APPROVE", "reviewed")
    sf.log.clear()
    original = sf.query
    schema_changed = not after_dml

    def query(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        nonlocal schema_changed
        if sql.startswith(f"UPDATE {SOURCE}"):
            schema_changed = True
        if schema_changed and "INFORMATION_SCHEMA.COLUMNS" in sql and params[0] == "DEMO":
            return [{"COLUMN_NAME": "SKU", "COLUMN_DEFAULT": "'NEW'"}]
        return original(sql, params)

    monkeypatch.setattr(sf, "query", query)
    audit = Mock(wraps=store.audit)
    monkeypatch.setattr(store, "audit", audit)
    with pytest.raises(WorkflowError, match=r"schema.*back to SUBMITTED"):
        engine.execute(rid, "admin")
    assert bool(sf.dml_statements()) == after_dml
    assert store.get(rid)["STATUS"] == "SUBMITTED"
    assert audit.call_args.args[1] == "DRIFT_BLOCKED"
    details = audit.call_args.args[3]
    assert details["reason"] == "SCHEMA_CHANGED"
    assert bool(details.get("query_id")) == after_dml
    assert bool(store.get(rid).get("EXEC_QUERY_ID")) == after_dml


@pytest.mark.parametrize("revalidated", [False, True])
def test_schema_check_reads_legacy_published_clone(
    engine: Engine,
    sf: FakeSnowflake,
    store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    revalidated: bool,
) -> None:
    rid = _submitted(engine)
    if revalidated:
        engine.revalidate(rid, "admin")
    row = store.requests[rid]
    for key in ("REVISION", "RUN_REVISION", "DRY_SANDBOX_REVISION", "REVAL_SANDBOX_REVISION"):
        row.pop(key, None)
    expected = row["SANDBOX_BASE"] + ("_RV" if revalidated else "")
    observed = []
    original = sf.query

    def legacy_clone_only(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "INFORMATION_SCHEMA.COLUMNS" in sql and params[0] == "SANDBOX":
            observed.append(params[1])
            if params[1] != expected:
                return []
        return original(sql, params)

    monkeypatch.setattr(sf, "query", legacy_clone_only)
    engine.review(rid, "admin", "APPROVE", "legacy schema")
    assert observed == [expected]


@pytest.mark.parametrize(
    ("error_code", "message", "reason"),
    [
        (707, "Time travel data is not available for table REGIONS.", "Time Travel history"),
        (
            91930,
            "Change tracking is not enabled or has been missing for the time range requested "
            "on table REGIONS.",
            "Ask the table owner to verify change tracking",
        ),
    ],
)
def test_unavailable_lookup_baseline_can_be_revalidated(
    engine: Engine,
    sf: FakeSnowflake,
    store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error_code: int,
    message: str,
    reason: str,
) -> None:
    monkeypatch.setattr(
        store, "load_config", lambda _: {**CONFIG, "ALLOWED_READ_TABLES": "APP.DEMO.REGIONS"}
    )
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    engine.dry_run(rid, "dev", "DELETE FROM pricing_config WHERE sku IN (SELECT code FROM regions)")
    engine.submit(rid, "dev")
    engine.review(rid, "admin", "APPROVE", "reviewed")
    original = sf.query

    class ExpiredHistoryError(RuntimeError):
        sql_error_code = error_code

    def expired(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if '"REGIONS" CHANGES(' in sql:
            raise ExpiredHistoryError(message)
        return original(sql, params)

    monkeypatch.setattr(sf, "query", expired)
    sf.log.clear()
    with pytest.raises(WorkflowError, match=reason):
        engine.execute(rid, "admin")
    assert sf.dml_statements() == []
    assert store.get(rid)["STATUS"] == "SUBMITTED"
    monkeypatch.setattr(sf, "query", original)
    engine.revalidate(rid, "admin")
    engine.review(rid, "admin", "APPROVE", "fresh baseline")


def test_unrelated_drift_read_failure_retains_execution_error(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _submitted(engine)
    engine.review(rid, "admin", "APPROVE", "reviewed")
    original = sf.query

    def denied(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if " CHANGES(" in sql:
            raise RuntimeError("insufficient privilege")
        return original(sql, params)

    monkeypatch.setattr(sf, "query", denied)
    with pytest.raises(RuntimeError, match="insufficient privilege"):
        engine.execute(rid, "admin")
    assert store.get(rid)["STATUS"] == "FAILED"
    assert not store.get(rid).get("EXEC_QUERY_ID")


@pytest.mark.parametrize("approved", [False, True])
def test_legacy_run_context_needs_fresh_revalidation(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore, approved: bool
) -> None:
    rid = _submitted(engine)
    assert store.get(rid)["RUN_CONTEXT_VERSION"] == DML_CONTEXT_VERSION
    if approved:
        engine.review(rid, "admin", "APPROVE", "earlier context")
    store.requests[rid].pop("RUN_CONTEXT_VERSION")
    sf.log.clear()
    with pytest.raises(WorkflowError, match="earlier SQL session settings"):
        if approved:
            engine.execute(rid, "admin")
        else:
            engine.review(rid, "admin", "APPROVE", "earlier context")
    assert store.get(rid)["STATUS"] == "SUBMITTED"
    assert not sf.dml_statements()
    engine.revalidate(rid, "admin")
    assert store.get(rid)["RUN_CONTEXT_VERSION"] == DML_CONTEXT_VERSION
    engine.review(rid, "admin", "APPROVE", "fresh context")
    assert engine.execute(rid, "admin")["status"] == "EXECUTED"


@pytest.mark.parametrize("drop_fails_once", [False, True])
def test_partial_initial_clone_after_concurrent_cleanup_is_reclaimed(
    sf: FakeSnowflake, store: MemoryStore, monkeypatch: pytest.MonkeyPatch, drop_fails_once: bool
) -> None:
    rid = "11111111-aaaa"
    engine = Engine(sf, store, new_id=lambda: rid)
    original = sf.query
    objects: set[str] = set()
    cleaned = False

    def interleave(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        nonlocal cleaned, drop_fails_once
        if sql.startswith("CREATE OR REPLACE TABLE") and not cleaned:
            engine.cancel(rid, "dev")
            assert engine.cleanup(0, "admin", rid)["dropped"] == 1
            cleaned = True
            objects.add(sql.split()[4])
        if sql.startswith("CREATE OR REPLACE STREAM"):
            raise RuntimeError("stream creation failed")
        if sql.startswith("DROP TABLE IF EXISTS"):
            if objects and drop_fails_once:
                drop_fails_once = False
                raise RuntimeError("clone temporarily busy")
            objects.discard(sql.split()[4])
        return original(sql, params)

    monkeypatch.setattr(sf, "query", interleave)
    with pytest.raises(RuntimeError, match="stream creation failed"):
        engine.create_request("PRICING_CONFIG", "dev")
    assert store.get(rid)["STATUS"] == "CANCELLED"
    if objects:
        assert store.get(rid)["SANDBOX_DROPPED_AT"] is None
        assert store.events[-1][1] == "SANDBOX_DROP_FAILED"
        assert engine.cleanup(0, "admin", rid)["dropped"] == 1
    assert not objects
    assert store.get(rid)["SANDBOX_DROPPED_AT"]


def test_legacy_revalidation_rejects_a_changed_timestamp_meaning(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    raw = "UPDATE pricing_config SET price = DAY(CAST(TO_TIMESTAMP_LTZ(1, 9) AS DATE))"
    rid = engine.create_request("PRICING_CONFIG", "dev")["request_id"]
    engine.dry_run(rid, "dev", raw)
    engine.submit(rid, "dev")
    row = store.requests[rid]
    row["CANONICAL_SQL"] = row["CANONICAL_SQL"].replace("TO_TIMESTAMP_LTZ", "TO_TIMESTAMP")
    row["SQL_SHA256"] = sha256(row["CANONICAL_SQL"])
    row["RUN_CONTEXT_VERSION"] = None
    revision = row["REVISION"]
    sf.log.clear()
    with pytest.raises(WorkflowError, match="new request"):
        engine.revalidate(rid, "admin")
    assert row["STATUS"] == "SUBMITTED" and row["RUN_CONTEXT_VERSION"] is None
    assert row["REVISION"] == revision and not sf.dml_statements()


def test_legacy_revalidation_needs_the_original_developer_sql(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore
) -> None:
    rid = _submitted(engine)
    store.requests[rid].update(RUN_CONTEXT_VERSION=None, SQL_TEXT=None)
    sf.log.clear()
    with pytest.raises(WorkflowError, match=r"original developer SQL.*new request"):
        engine.revalidate(rid, "admin")
    assert not sf.dml_statements() and store.get(rid)["RUN_CONTEXT_VERSION"] is None


def test_create_does_not_report_a_clone_removed_by_concurrent_cancellation(
    engine: Engine, sf: FakeSnowflake, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = "11111111-aaaa"
    original = sf.query
    objects: set[str] = set()

    def interleave(sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if sql.startswith("CREATE OR REPLACE TABLE"):
            engine.cancel(rid, "dev")
            assert engine.cleanup(0, "admin", rid)["dropped"] == 1
            objects.add(sql.split()[4])
        elif sql.startswith("CREATE OR REPLACE STREAM"):
            objects.add(sql.split()[4])
        elif sql.startswith(("DROP TABLE IF EXISTS", "DROP STREAM IF EXISTS")):
            objects.discard(sql.split()[4])
        return original(sql, params)

    monkeypatch.setattr(sf, "query", interleave)
    with pytest.raises(WorkflowError, match="CANCELLED"):
        engine.create_request("PRICING_CONFIG", "dev")
    assert not objects and store.get(rid)["SANDBOX_DROPPED_AT"]
