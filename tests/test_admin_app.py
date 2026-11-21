"""UI behaviour of the admin app, driven headlessly with AppTest."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import streamlit as st
from fakes import CONFIG, DRY_RUN_DONE, FakeRepository, make_request
from streamlit.testing.v1 import AppTest

from auditable_crud import ui
from auditable_crud.config import TableConfig
from auditable_crud.repository import ADMIN_API, ApiError, Repository, RequestView
from auditable_crud.runner import SqlRunner

APP = str(Path(__file__).parents[1] / "streamlit" / "admin_app" / "streamlit_app.py")


def _review(repo: FakeRepository, **request: object) -> AppTest:
    repo.requests_by_id["req-1"] = make_request(**{**DRY_RUN_DONE, **request})
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["repository"] = repo
    at.session_state["actor"] = repo.user
    at.session_state["review_id"] = "req-1"
    at.run()
    return at.switch_page("app_pages/review.py").run()


def _button(at: AppTest, label: str) -> object:
    return next(b for b in at.button if b.label == label)


def _select_first_request(at: AppTest) -> None:
    # AppTest 1.52 exposes dataframe contents but has no row-click method.
    selection = at._tree.get_widget_states()
    selection.widgets.add(
        id=at.dataframe[0].proto.id,
        string_value='{"selection":{"rows":[0],"columns":[],"cells":[]}}',
    )
    at._run(selection)


def test_queue_shows_open_requests_and_own_count() -> None:
    repo = FakeRepository(user="admin")
    repo.requests_by_id = {
        "a": make_request(REQUEST_ID="a", STATUS="SUBMITTED", REQUESTED_BY="dev"),
        "b": make_request(REQUEST_ID="b", STATUS="APPROVED", REQUESTED_BY="ADMIN"),
        "c": make_request(REQUEST_ID="c", STATUS="EXECUTED", REQUESTED_BY="dev"),
    }
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["repository"] = repo
    at.session_state["actor"] = "admin"
    at.run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {
        "Waiting for review": "1",
        "Approved, not executed": "1",
        "Mine (cannot review)": "1",
    }
    at.multiselect(key="queue_tables").set_value(["PRICING_CONFIG"]).run()
    assert not at.exception
    table_summary = at.dataframe[0].value.iloc[0]
    assert table_summary["Open requests"] == 2 and table_summary["Business key"] == "SKU"


def test_empty_queue_shows_all_caught_up() -> None:
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["repository"] = FakeRepository(user="admin")
    at.session_state["actor"] = "admin"
    at.run()
    assert not at.exception
    assert "All caught up" in at.success[0].value


def test_user_text_cannot_inject_markdown_into_the_admin_view() -> None:
    note = "[approve me](https://evil.example) :red[URGENT] **bold**"
    at = _review(FakeRepository(user="admin"), STATUS="SUBMITTED", SUBMIT_NOTE=note)
    rendered = at.info[0].value
    assert "[approve me](" not in rendered
    assert ":red[" not in rendered
    assert r"\[approve me\]\(https\:" in rendered


def test_requester_cannot_decide_on_own_request() -> None:
    at = _review(FakeRepository(user="dev"), STATUS="SUBMITTED")
    assert not at.exception
    assert "another admin must re-validate, approve, reject" in at.warning[0].value
    assert _button(at, "Re-validate on current data").disabled  # type: ignore[attr-defined]
    assert all(b.disabled for b in at.button if b.label in {"Approve", "Reject"})


def test_approve_requires_comment_then_sends_decision() -> None:
    repo = FakeRepository(user="admin")
    at = _review(repo, STATUS="SUBMITTED")
    _button(at, "Approve").click().run()  # type: ignore[attr-defined]
    assert "add a comment" in at.error[0].value and repo.ops() == []

    at.text_area(key="review_comment_req-1").input("Checked with pricing team").run()
    _button(at, "Approve").click().run()  # type: ignore[attr-defined]
    assert repo.calls[-1] == (
        "review",
        {
            "actor": "admin",
            "request_id": "req-1",
            "decision": "APPROVE",
            "comment": "Checked with pricing team",
            "tolerance_pct": 0.0,
            "expected_revision": 0,
        },
    )


def test_drift_panel_flags_overlap() -> None:
    repo = FakeRepository(user="admin")
    published = str(RequestView(make_request(DRY_SANDBOX_REVISION=8)).sandbox("diff"))
    repo.responses["drift"] = {
        "baseline_ns": 1_700_000_000_000_000_000,
        "end_ns": 1_700_000_000_000_000_123,
        "diff_fqn": published,
        "source_fqn": "APP.DEMO.PRICING_CONFIG",
        "key_columns": ["SKU"],
        "revision": 8,
        "inserted": 0,
        "updated": 2,
        "deleted": 0,
        "total": 2,
        "overlapping_keys": 1,
        "classification": "DRIFT_OVERLAPS",
        "revalidated": False,
    }
    detail = Mock(wraps=repo.drift_rows)
    repo.drift_rows = detail  # type: ignore[method-assign]
    at = _review(repo, STATUS="SUBMITTED", REVISION=8, RUN_REVISION=8, DRY_SANDBOX_REVISION=8)
    at.radio(key="inspect").set_value("Drift since dry run").run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Overlap with this change"] == "1"
    assert any("Re-validate" in w.value for w in at.warning)
    assert str(detail.call_args.args[2]) == published
    assert detail.call_args.kwargs["end_ns"] == repo.responses["drift"]["end_ns"]
    repo.responses["drift"]["revision"] = 9
    detail.reset_mock()
    at.run()
    assert not at.exception and not detail.called
    assert any("Refresh" in warning.value for warning in at.warning)
    # Publishing a claimed run changes its artifact without incrementing REVISION again.
    repo.responses["drift"].update(revision=8, diff_fqn="APP.SANDBOX.NEWER_DIFF")
    at.run()
    assert not at.exception and not detail.called


@pytest.mark.parametrize("override", [{"SOURCE_TABLE": "OTHER_CONFIG"}, {"KEY_COLUMNS": "REGION"}])
def test_drift_panel_suppresses_details_for_stale_managed_table_settings(
    override: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = TableConfig.from_row({**CONFIG, **override})
    request = RequestView(make_request(**DRY_RUN_DONE))
    report = {
        "revision": request.revision,
        "diff_fqn": str(request.sandbox("diff")),
        "source_fqn": "APP.DEMO.PRICING_CONFIG",
        "key_columns": ["SKU"],
        "baseline_ns": 100,
        "end_ns": 200,
        "classification": "DRIFT_OVERLAPS",
        "total": 1,
        "inserted": 1,
        "updated": 0,
        "deleted": 0,
        "overlapping_keys": 1,
    }
    monkeypatch.setattr(ui, "call_api", lambda *args, **kwargs: report)
    warning, metric, repo = Mock(), Mock(), Mock()
    monkeypatch.setattr(st, "warning", warning)
    monkeypatch.setattr(st, "metric", metric)
    assert ui.drift_panel(repo, request, cached, key="stale") == {}
    assert "Refresh" in warning.call_args.args[0]
    metric.assert_not_called()
    repo.drift_rows.assert_not_called()


def test_execute_requires_confirmation_and_only_for_approved_requests() -> None:
    repo = FakeRepository(user="admin")
    at = _review(repo, STATUS="APPROVED", REVIEWED_BY="admin2", REVIEW_COMMENT="ok")
    _button(at, "Execute on APP.DEMO.PRICING_CONFIG").click().run()  # type: ignore[attr-defined]
    assert repo.ops() == []  # first click only asks
    assert any("cannot be undone" in w.value for w in at.warning)
    _button(at, "Yes, execute now").click().run()  # type: ignore[attr-defined]
    assert repo.calls[-1] == (
        "execute",
        {"actor": "admin", "request_id": "req-1", "expected_revision": 0},
    )

    submitted = _review(FakeRepository(user="admin"), STATUS="SUBMITTED")
    assert not any(b.label.startswith("Execute on") for b in submitted.button)


def test_reject_asks_for_confirmation_then_sends_decision() -> None:
    repo = FakeRepository(user="admin")
    at = _review(repo, STATUS="SUBMITTED")
    at.text_area(key="review_comment_req-1").input("Wrong region").run()
    _button(at, "Reject").click().run()  # type: ignore[attr-defined]
    assert repo.ops() == []
    _button(at, "Yes, reject").click().run()  # type: ignore[attr-defined]
    assert repo.calls[-1][1]["decision"] == "REJECT"


def test_missing_request_shows_message_not_traceback() -> None:
    repo = FakeRepository(user="admin")
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["repository"] = repo
    at.session_state["actor"] = "admin"
    at.session_state["review_id"] = "gone"
    at.run()
    at = at.switch_page("app_pages/review.py").run()
    assert not at.exception
    assert "no longer exists" in at.warning[0].value


def test_unmanaged_table_shows_message_not_traceback() -> None:
    at = _review(FakeRepository(user="admin"), STATUS="SUBMITTED", TABLE_ID="dropped_table")
    assert not at.exception
    assert "no longer managed" in at.error[0].value


def test_return_to_review_requires_a_reason_and_an_independent_admin() -> None:
    repo = FakeRepository(user="admin")
    at = _review(repo, STATUS="APPROVED", REVIEWED_BY="admin2", REVIEW_COMMENT="ok")
    assert next(b for b in at.button if b.label == "Return to review").disabled
    at.text_input(key="send_back_reason_req-1_0").input("Refresh the baseline").run()
    next(b for b in at.button if b.label == "Return to review").click().run()
    assert not at.exception
    assert repo.calls[-1] == (
        "send_back",
        {
            "actor": "admin",
            "request_id": "req-1",
            "comment": "Refresh the baseline",
            "expected_revision": 0,
        },
    )
    own = _review(FakeRepository(user="dev"), STATUS="APPROVED")
    assert next(b for b in own.button if b.label == "Return to review").disabled


def test_return_to_review_cannot_reopen_a_later_approval_from_an_old_screen() -> None:
    repo = FakeRepository(user="admin")
    at = _review(repo, STATUS="APPROVED", REVISION=1, RUN_REVISION=1, REVIEWED_BY="reviewer")
    at.text_input(key="send_back_reason_req-1_1").input("Concern about revision one").run()
    repo.requests_by_id["req-1"].update(REVISION=3, RUN_REVISION=3, REVIEWED_BY="new reviewer")
    next(b for b in at.button if b.label == "Return to review").click().run()
    assert not at.exception and not repo.calls
    assert at.text_input(key="send_back_reason_req-1_3").value == ""
    assert next(b for b in at.button if b.label == "Return to review").disabled


def test_cleaned_request_retains_counts_and_audit_without_accessing_dropped_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepository(user="admin")
    monkeypatch.setattr(repo, "diff_page", lambda *_: pytest.fail("queried removed diff"))
    at = _review(repo, STATUS="EXECUTED", SANDBOX_DROPPED_AT="2026-01-01")
    assert not at.exception
    assert any("cleanup removed" in caption.value for caption in at.caption)
    assert "Drift since dry run" not in at.radio(key="inspect").options
    assert not repo.calls
    at.radio(key="inspect").set_value("Audit trail").run()
    assert list(at.dataframe[0].value["EVENT"]) == ["CREATED"]


def test_expired_snapshot_warns_and_an_incomplete_revalidation_disables_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepository(user="admin")
    monkeypatch.setattr(repo, "table_stats", lambda _: {"ROW_COUNT": 10, "RETENTION_TIME": 1})
    at = _review(repo, STATUS="SUBMITTED", REVISION=2, RUN_REVISION=1)
    assert not at.exception
    assert any("Time Travel retention" in w.value for w in at.warning)
    assert next(b for b in at.button if b.label == "Approve").disabled
    assert not next(b for b in at.button if b.label == "Re-validate on current data").disabled


@pytest.mark.parametrize(
    ("details", "query_id"),
    [
        ('{"revision":3,"query_id":"revalidation-query","elapsed_ms":38}', "revalidation-query"),
        ('{"elapsed_ms":38}', None),
    ],
)
def test_admin_history_combines_table_actor_and_status_filters(
    monkeypatch: pytest.MonkeyPatch, details: str, query_id: str | None
) -> None:
    repo = FakeRepository(user="admin")
    repo.requests_by_id["old"] = make_request(
        **{
            **DRY_RUN_DONE,
            "REQUEST_ID": "old",
            "STATUS": "EXECUTED",
            "REQUESTED_BY": "DeV",
            "REVAL_SANDBOX_REVISION": 3,
            "REVAL_COUNTS": DRY_RUN_DONE["DRY_RUN_COUNTS"],
            "EXEC_COUNTS": DRY_RUN_DONE["DRY_RUN_COUNTS"],
            "EXEC_QUERY_ID": "exec-query",
        }
    )
    repo.requests_by_id["new"] = make_request(
        REQUEST_ID="new", STATUS="SUBMITTED", TABLE_ID="FEATURE_FLAGS"
    )
    audit = Mock(
        return_value=pd.DataFrame(
            [
                {"EVENT": "CREATED", "DETAILS": "{}"},
                {"EVENT": "REVALIDATED", "DETAILS": '{"revision":2,"query_id":"previous"}'},
                {"EVENT": "REVALIDATED", "DETAILS": details},
            ]
        )
    )
    monkeypatch.setattr(repo, "audit", audit)
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["repository"] = repo
    at.session_state["actor"] = repo.user
    at.run()
    at.switch_page("app_pages/history.py").run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Requests"] == "2"
    at.multiselect(key="hist_tables").set_value(["PRICING_CONFIG"]).run()
    at.multiselect(key="hist_status").set_value(["EXECUTED"]).run()
    at.text_input(key="hist_user").input("DEV").run()
    assert not at.exception
    assert list(at.dataframe[0].value["REQUEST_ID"]) == ["old"]
    _select_first_request(at)
    assert not at.exception
    assert any(s.value == "Request details" for s in at.subheader)
    assert list(at.dataframe[-1].value["EVENT"]) == ["CREATED", "REVALIDATED", "REVALIDATED"]
    stages = at.dataframe[-2].value.set_index("Stage")
    assert list(stages["Query ID"]) == ["01-q", query_id, "exec-query"]
    assert stages.at["Re-validation", "Elapsed (ms)"] == 38
    audit.assert_called_once_with("old")
    at.text_input(key="hist_user").input("no matches").run()
    assert not at.exception and at.dataframe[0].value.empty
    assert not any(s.value == "Request details" for s in at.subheader)


def test_another_admins_revalidation_invalidates_the_displayed_approval_button() -> None:
    repo = FakeRepository(user="admin")
    at = _review(repo, STATUS="SUBMITTED", REVISION=1, RUN_REVISION=1)
    at.text_area(key="review_comment_req-1").input("Reviewed the first run").run()
    repo.requests_by_id["req-1"].update(REVISION=2, RUN_REVISION=2)
    next(b for b in at.button if b.label == "Approve").click().run()
    assert not at.exception and not repo.calls
    assert any("request has changed" in m.value for m in at.info)
    next(b for b in at.button if b.label == "Approve").click().run()
    assert repo.calls[-1][1]["expected_revision"] == 2


@pytest.mark.parametrize(
    ("status", "counts", "stage"),
    [
        ("FAILED", None, "Failed execution"),
        ("SUBMITTED", {"updated": 2, "total": 2}, "Rolled back"),
    ],
)
def test_history_shows_failed_and_rolled_back_source_attempts(
    status: str, counts: dict[str, int] | None, stage: str
) -> None:
    at = _review(
        FakeRepository("admin"),
        STATUS=status,
        EXEC_QUERY_ID="source-query",
        EXEC_ELAPSED_MS=26,
        EXEC_COUNTS=counts,
    )
    at.switch_page("app_pages/history.py").run()
    _select_first_request(at)
    assert not at.exception
    stages = at.dataframe[-2].value.set_index("Stage")
    assert stages.at[stage, "Query ID"] == "source-query" and stages.at[stage, "Elapsed (ms)"] == 26


def test_drift_details_display_exact_nullable_source_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepository("admin")
    repo.responses["drift"] = {
        "baseline_ns": 1,
        "end_ns": 2,
        "diff_fqn": str(RequestView(make_request()).sandbox("diff")),
        "source_fqn": "APP.DEMO.PRICING_CONFIG",
        "key_columns": ["SKU"],
        "revision": 0,
        "inserted": 2,
        "updated": 0,
        "deleted": 0,
        "total": 2,
        "overlapping_keys": 1,
        "classification": "DRIFT_OVERLAPS",
        "revalidated": False,
    }
    runner = Mock(spec=SqlRunner)
    metadata = [{"COLUMN_NAME": name, "DATA_TYPE": "NUMBER"} for name in ("SKU", "PRICE")]
    flags = {"AC_ACTION": "INSERT", "AC_ISUPDATE": False, "AC_OVERLAPS": True}
    runner.query.side_effect = [
        metadata,
        [
            {**flags, "SKU": 2**53 + 1, "PRICE": 10**37 + 1},
            {**flags, "SKU": None, "PRICE": None},
        ],
    ]
    monkeypatch.setattr(
        repo, "drift_rows", Repository(runner, '"APP"."CORE"', ADMIN_API).drift_rows
    )
    at = _review(repo, STATUS="SUBMITTED")
    at.radio(key="inspect").set_value("Drift since dry run").run()
    assert not at.exception
    details = at.dataframe[0].value
    assert details.at[0, "SKU"] == str(2**53 + 1) and details.at[0, "PRICE"] == str(10**37 + 1)


def test_empty_revalidation_page_cannot_stand_in_for_changed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepository("admin")
    monkeypatch.setattr(repo, "diff_page", lambda *_: pd.DataFrame())
    at = _review(
        repo,
        STATUS="SUBMITTED",
        REVISION=4,
        RUN_REVISION=4,
        REVAL_SNAPSHOT_NS=1_900_000_000_000_000_000,
        REVAL_SANDBOX_REVISION=4,
        REVAL_COUNTS={"updated": 201, "total": 201},
        REVAL_MATCHES=False,
    )
    assert next(b for b in at.button if b.label == "Approve").disabled
    at.number_input(key="admin_reval_diff_req-1_4_updated_page").set_value(2).run()
    repo.requests_by_id["req-1"].update(
        REVISION=5,
        RUN_REVISION=5,
        REVAL_SANDBOX_REVISION=5,
        REVAL_COUNTS={"inserted": 201, "updated": 0, "total": 201},
    )
    at.run()
    assert at.radio(key="admin_reval_diff_req-1_5_kind").value == "inserted"
    assert at.number_input(key="admin_reval_diff_req-1_5_inserted_page").value == 1
    at.radio(key="admin_reval_diff_req-1_5_kind").set_value("updated").run()
    assert next(b for b in at.button if b.label == "Approve").disabled
    repo.requests_by_id["req-1"].update(
        REVISION=6,
        RUN_REVISION=6,
        REVAL_SANDBOX_REVISION=6,
        REVAL_COUNTS={"inserted": 0, "updated": 0, "deleted": 0, "total": 0},
    )
    at.run()
    assert not at.exception
    assert any("no row changes" in caption.value for caption in at.caption)
    assert not next(b for b in at.button if b.label == "Approve").disabled


def test_revalidated_change_set_opens_as_the_approval_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepository(user="admin")
    monkeypatch.setattr(
        repo,
        "diff_page",
        lambda diff, *_: pd.DataFrame(
            [
                {
                    "SKU (before)": "A",
                    "SKU (after)": "B" if "_R4_DIFF" in diff.name else "C",
                }
            ]
        ),
    )
    at = _review(
        repo,
        STATUS="SUBMITTED",
        REVISION=4,
        RUN_REVISION=4,
        DRY_SANDBOX_REVISION=1,
        REVAL_SANDBOX_REVISION=4,
        REVAL_SNAPSHOT_NS=1_900_000_000_000_000_000,
        REVAL_COUNTS=DRY_RUN_DONE["DRY_RUN_DIFF_COUNTS"],
        REVAL_MATCHES=False,
    )
    assert not at.exception
    assert at.radio(key="evidence_req-1_4").value == "reval_diff"
    assert any("_R4_DIFF" in c.value for c in at.caption)
    at.radio(key="evidence_req-1_4").set_value("diff").run()
    assert at.dataframe[0].value.at[0, "SKU (after)"] == "C"
    assert next(b for b in at.button if b.label == "Approve").disabled
    at.text_area(key="review_comment_req-1").input("Reviewed the current change set").run()
    at.radio(key="inspect").set_value("Audit trail").run()
    assert next(b for b in at.button if b.label == "Approve").disabled and not repo.calls
    at.radio(key="inspect").set_value("Change set").run()
    at.radio(key="evidence_req-1_4").set_value("reval_diff").run()
    assert at.dataframe[0].value.at[0, "SKU (after)"] == "B"
    next(b for b in at.button if b.label == "Approve").click().run()
    assert repo.calls[-1][1]["expected_revision"] == 4


def test_execution_failure_shows_the_new_request_state(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepository(user="admin")
    at = _review(repo, STATUS="APPROVED", REVIEWED_BY="admin2", REVIEW_COMMENT="ok")
    next(b for b in at.button if b.label.startswith("Execute on")).click().run()

    def drift_blocked(*_args: object, **_kwargs: object) -> None:
        repo.requests_by_id["req-1"].update(STATUS="SUBMITTED", REVISION=2)
        raise ApiError("Drift after approval")

    denied = Mock(side_effect=drift_blocked)
    monkeypatch.setattr(repo, "call", denied)
    next(b for b in at.button if b.label == "Yes, execute now").click().run()
    assert not at.exception
    assert not any(b.label.startswith("Execute on") for b in at.button)
    assert any("Drift after approval" in m.value for m in at.error)
    assert any(b.label == "Re-validate on current data" for b in at.button)
