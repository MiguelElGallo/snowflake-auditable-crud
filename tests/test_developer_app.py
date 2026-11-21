"""UI behaviour of the developer app, driven headlessly with AppTest."""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
import streamlit as st
from fakes import DRY_RUN_DONE, FakeRepository, make_request
from streamlit.connections import SnowflakeConnection
from streamlit.runtime.caching import cache_resource_api
from streamlit.testing.v1 import AppTest

from auditable_crud import ui
from auditable_crud.repository import DEVELOPER_API, Repository
from auditable_crud.runner import SqlRunner

APP = str(Path(__file__).parents[1] / "streamlit" / "developer_app" / "streamlit_app.py")


def _app(repo: FakeRepository, request_id: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["repository"] = repo
    at.session_state["actor"] = repo.user
    if request_id:
        at.session_state["request_id"] = request_id
    return at.run()


def test_first_screen_lists_every_managed_table() -> None:
    at = _app(FakeRepository())
    assert not at.exception
    assert [s.value for s in at.subheader] == ["PRICING_CONFIG", "FEATURE_FLAGS"]
    assert [b.label for b in at.button] == ["Propose a change"] * 2


def test_duplicate_manifest_ids_hide_only_the_ambiguous_choice() -> None:
    repo = FakeRepository()
    repo.configs.append(repo.configs[0])
    at = _app(repo)
    assert not at.exception and "PRICING_CONFIG" in at.error[0].value
    assert [b.key for b in at.button] == ["pick_FEATURE_FLAGS"]


def test_remapped_request_never_opens_the_new_source_in_the_workbench() -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(SOURCE_FQN="APP.DEMO.PREVIOUS_SOURCE")
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    assert not at.exception and "different source" in at.error[0].value
    assert not at.text_area and not repo.calls


def test_choosing_a_table_clones_it_and_opens_the_workbench() -> None:
    repo = FakeRepository()
    cfg = next(cfg for cfg in repo.configs if cfg.table_id == "FEATURE_FLAGS")
    repo.requests_by_id["new-req"] = make_request(
        REQUEST_ID="new-req",
        TABLE_ID=cfg.table_id,
        SOURCE_FQN=str(cfg.source),
        SANDBOX_BASE="FEATURE_FLAGS__CR_NEW",
    )
    repo.responses["create_request"] = {
        "request_id": "new-req",
        "clone": str(repo.request("new-req").sandbox("clone")),
    }
    at = _app(repo)
    at.button(key="pick_FEATURE_FLAGS").click().run()
    assert not at.exception
    assert repo.calls == [("create_request", {"actor": "dev", "table_id": "FEATURE_FLAGS"})]
    assert at.session_state["request_id"] == "new-req"
    assert at.title[0].value == "Change workbench"
    assert at.text_area(key="sql_editor").label == "DML against APP.DEMO.FEATURE_FLAGS"


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("DROP TABLE pricing_config", "Only INSERT"),
        ("UPDATE feature_flags SET enabled = TRUE", "modifies"),
    ],
)
def test_workbench_blocks_invalid_sql_before_any_call(sql: str, message: str) -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request()
    at = _app(repo, "req-1")
    at.switch_page("app_pages/workbench.py").run()
    at.text_area(key="sql_editor").input(sql).run()
    assert message in at.error[0].value
    dry_run = next(b for b in at.button if b.label == "Dry run on the clone")
    assert dry_run.disabled
    assert repo.ops() == []


def test_valid_sql_dry_runs_with_the_request_and_sql() -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request()
    at = _app(repo, "req-1")
    at.switch_page("app_pages/workbench.py").run()
    at.text_area(key="sql_editor").input("UPDATE pricing_config SET price = 1").run()
    assert "Valid UPDATE" in at.success[0].value
    next(b for b in at.button if b.label == "Dry run on the clone").click().run()
    assert repo.calls[-1] == (
        "dry_run",
        {
            "actor": "dev",
            "request_id": "req-1",
            "sql": "UPDATE pricing_config SET price = 1",
            "nl_prompt": None,
        },
    )


def test_dry_run_results_and_submit() -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(**DRY_RUN_DONE)
    at = _app(repo, "req-1")
    at.switch_page("app_pages/workbench.py").run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Updated"] == "3" and metrics["Total affected"] == "3"
    next(b for b in at.button if b.label == "Submit for approval").click().run()
    assert repo.calls[-1] == (
        "submit",
        {
            "actor": "dev",
            "request_id": "req-1",
            "note": "",
            "sql": DRY_RUN_DONE["SQL_TEXT"],
            "expected_revision": 0,
        },
    )


def test_sql_edited_after_dry_run_cannot_be_submitted() -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(**DRY_RUN_DONE)
    at = _app(repo, "req-1")
    at.switch_page("app_pages/workbench.py").run()
    at.text_area(key="sql_editor").input("DELETE FROM pricing_config").run()
    assert any("differs from the one that was dry-run" in w.value for w in at.warning)
    assert next(b for b in at.button if b.label == "Submit for approval").disabled
    assert repo.ops() == []


def test_unsaved_editor_state_survives_navigation_and_stays_with_its_request() -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(**DRY_RUN_DONE)
    repo.requests_by_id["other"] = make_request(REQUEST_ID="other")
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    sql = "UPDATE pricing_config SET price = 42 WHERE sku = 'B'"
    at.text_area(key="sql_editor").input(sql).run()
    at.text_input(key="submit_note_req-1").input("Please check SKU B").run()
    at.switch_page("app_pages/my_requests.py").run()
    at.session_state["request_id"] = "other"
    at.switch_page("app_pages/workbench.py").run()
    assert at.text_area(key="sql_editor").value == ""
    at.switch_page("app_pages/my_requests.py").run()
    at.session_state["request_id"] = "req-1"
    at.switch_page("app_pages/workbench.py").run()
    assert not at.exception and at.text_area(key="sql_editor").value == sql
    assert at.text_input(key="submit_note_req-1").value == "Please check SKU B"
    assert next(b for b in at.button if b.label == "Submit for approval").disabled
    assert repo.ops() == [] and repo.requests_by_id["req-1"]["SQL_TEXT"] == DRY_RUN_DONE["SQL_TEXT"]


@pytest.mark.parametrize("fields", [{}, DRY_RUN_DONE], ids=["draft", "dry_run"])
def test_discard_asks_for_confirmation(fields: dict[str, object]) -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(**fields)
    at = _app(repo, "req-1")
    at.switch_page("app_pages/workbench.py").run()
    next(b for b in at.button if b.label == "Discard request").click().run()
    assert repo.ops() == []
    next(b for b in at.button if b.label == "Yes, discard").click().run()
    assert repo.ops() == ["cancel"]
    assert at.session_state["request_id"] is None
    assert at.title[0].value == "Choose table"


def test_missing_request_returns_to_table_choice() -> None:
    at = _app(FakeRepository(), "gone")
    at.switch_page("app_pages/workbench.py").run()
    assert not at.exception
    assert "no longer exists" in at.warning[0].value


def test_zero_row_dry_run_cannot_be_submitted() -> None:
    repo = FakeRepository()
    zero = {"inserted": 0, "updated": 0, "deleted": 0, "total": 0}
    repo.requests_by_id["req-1"] = make_request(
        **{**DRY_RUN_DONE, "DRY_RUN_COUNTS": zero, "DRY_RUN_DIFF_COUNTS": zero}
    )
    at = _app(repo, "req-1")
    at.switch_page("app_pages/workbench.py").run()
    assert "changed no rows" in at.warning[0].value
    assert next(b for b in at.button if b.label == "Submit for approval").disabled


def test_cortex_generation_fills_the_editor_and_records_the_prompt() -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request()
    at = _app(repo, "req-1")
    at.switch_page("app_pages/workbench.py").run()
    at.radio(key="mode").set_value("Ask Cortex").run()
    at.text_area(key="nl_prompt").input("raise EU prices 5%").run()
    next(b for b in at.button if b.label == "Generate SQL").click().run()
    at.switch_page("app_pages/my_requests.py").run()
    at.switch_page("app_pages/workbench.py").run()
    assert at.radio(key="mode").value == "Ask Cortex"
    assert at.text_area(key="nl_prompt").value == "raise EU prices 5%"
    assert str(at.text_area(key="sql_editor").value).startswith("UPDATE PRICING_CONFIG SET price")
    next(b for b in at.button if b.label == "Dry run on the clone").click().run()
    assert repo.calls[-1][1]["nl_prompt"] == "raise EU prices 5%"

    at.text_area(key="sql_editor").input("DELETE FROM pricing_config WHERE price = 0").run()
    next(b for b in at.button if b.label == "Dry run on the clone").click().run()
    assert repo.calls[-1][1]["nl_prompt"] is None


@pytest.mark.parametrize("current_status", ["DRAFT", "SUBMITTED"])
def test_generate_click_after_reconnect_checks_the_live_request(
    monkeypatch: pytest.MonkeyPatch, current_status: str
) -> None:
    original, restored = FakeRepository(), FakeRepository()
    original.requests_by_id["req-1"] = make_request()
    restored.requests_by_id["req-1"] = make_request(STATUS=current_status)
    closed_generate = Mock(side_effect=RuntimeError("closed Snowflake session"))
    sql = "UPDATE pricing_config SET price = 42"
    live_generate = Mock(return_value=sql)
    monkeypatch.setattr(original, "generate_sql", closed_generate)
    monkeypatch.setattr(restored, "generate_sql", live_generate)
    monkeypatch.setattr(ui, "Repository", Mock(side_effect=[original, restored]))

    session_id = "workbench-generate-on-reconnect"
    monkeypatch.setattr(cache_resource_api, "get_session_id_or_throw", lambda: session_id)
    sessions = [Mock(session_id=1111), Mock(session_id=2222)]
    for session in sessions:
        session.get_current_database.return_value = '"APP"'
    monkeypatch.setattr(SnowflakeConnection, "_connect", Mock())
    monkeypatch.setattr(SnowflakeConnection, "session", Mock(side_effect=sessions))
    app = AppTest.from_file(APP, default_timeout=15)
    app.user["user_name"] = "dev"
    app.session_state["request_id"] = "req-1"
    try:
        app.run().switch_page("app_pages/workbench.py").run()
        app.radio(key="mode").set_value("Ask Cortex").run()
        prompt = "Set the price to 42"
        app.text_area(key="nl_prompt").input(prompt).run()
        button = next(b for b in app.button if b.label == "Generate SQL")
        cache_resource_api.clear_session_cache(session_id)
        sessions[0].close.assert_called_once_with()
        button.click().run()
        assert not app.exception
        assert app.session_state["snowflake_session_id"] == 2222
        closed_generate.assert_not_called()
        if current_status == "DRAFT":
            live_generate.assert_called_once_with(restored.configs[0], prompt)
            assert app.text_area(key="sql_editor").value == sql
            assert app.session_state["workbench_draft_req-1"]["generated_from"] == prompt
        else:
            live_generate.assert_not_called()
            assert app.text_area(key="sql_editor").disabled
    finally:
        cache_resource_api.clear_session_cache(session_id)


def test_workbench_only_opens_requests_owned_by_the_viewer() -> None:
    repo = FakeRepository(user="another developer")
    repo.requests_by_id["req-1"] = make_request(**DRY_RUN_DONE)
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    assert not at.exception
    assert any("unavailable to you" in message.value for message in at.warning)
    assert not at.text_area and not at.dataframe
    assert at.session_state["request_id"] is None


def test_incomplete_rerun_blocks_stale_submission() -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(**DRY_RUN_DONE, REVISION=3, RUN_REVISION=2)
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    assert not at.exception
    assert next(b for b in at.button if b.label == "Submit for approval").disabled
    assert any("has not completed" in warning.value for warning in at.warning)


def test_empty_cortex_answer_preserves_the_existing_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(**DRY_RUN_DONE)
    monkeypatch.setattr(repo, "generate_sql", lambda *_: "")
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    at.radio(key="mode").set_value("Ask Cortex").run()
    at.text_area(key="nl_prompt").input("adjust slowly").run()
    next(b for b in at.button if b.label == "Generate SQL").click().run()
    assert not at.exception
    assert at.text_area(key="sql_editor").value == DRY_RUN_DONE["SQL_TEXT"]
    assert any("did not return" in error.value for error in at.error)
    assert not repo.calls


def test_my_requests_filters_status_and_keeps_other_viewers_out() -> None:
    repo = FakeRepository()
    for rid, status, user in [
        ("mine-open", "SUBMITTED", "dev"),
        ("mine-done", "EXECUTED", "dev"),
        ("other", "SUBMITTED", "other"),
    ]:
        repo.requests_by_id[rid] = make_request(REQUEST_ID=rid, STATUS=status, REQUESTED_BY=user)
    at = _app(repo).switch_page("app_pages/my_requests.py").run()
    assert not at.exception
    assert list(at.dataframe[0].value["REQUEST_ID"]) == ["mine-open"]
    at.multiselect(key="my_status").set_value(["EXECUTED"]).run()
    assert not at.exception
    assert list(at.dataframe[0].value["REQUEST_ID"]) == ["mine-done"]


def test_change_set_uses_the_saved_revision_and_compares_typed_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request(**DRY_RUN_DONE, DRY_SANDBOX_REVISION=2)
    stored_columns = Mock(return_value=["ORIGINAL_COLUMN", "PRICE", "CLIENT_ID", "WIDE_NUMBER"])
    monkeypatch.setattr(repo, "diff_columns", stored_columns)
    runner = Mock(spec=SqlRunner)
    runner.query.return_value = [
        {
            "B0": None,
            "A0": "None",
            "B1": Decimal("1.00"),
            "A1": Decimal("1"),
            "B2": 2**53,
            "A2": 2**53 + 1,
            "B3": 10**37,
            "A3": 10**37 + 1,
        },
        dict.fromkeys(["B0", "A0", "B1", "A1", "B2", "A2", "B3", "A3"]),
    ]
    real = Repository(runner, '"APP"."CORE"', DEVELOPER_API)
    monkeypatch.setattr(repo, "diff_page", real.diff_page)
    download = Mock(wraps=st.download_button)
    monkeypatch.setattr(st, "download_button", download)
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    assert not at.exception
    assert stored_columns.call_args.args[0].name.endswith("_R2_DIFF")
    frame = at.dataframe[0].value
    assert list(frame["Changed columns"]) == ["ORIGINAL_COLUMN, CLIENT_ID, WIDE_NUMBER", ""]
    assert frame.at[0, "CLIENT_ID (before)"] == str(2**53)
    assert frame.at[0, "CLIENT_ID (after)"] == str(2**53 + 1)
    assert frame.at[0, "WIDE_NUMBER (after)"] == str(10**37 + 1)
    csv_rows = list(csv.DictReader(io.StringIO(download.call_args.args[1].decode())))
    assert csv_rows[0]["CLIENT_ID (after)"] == str(2**53 + 1)
    assert csv_rows[0]["WIDE_NUMBER (after)"] == str(10**37 + 1)
    assert csv_rows[0]["PRICE (before)"] == "1.00" and csv_rows[1]["CLIENT_ID (before)"] == ""


@pytest.mark.parametrize("kind", ["inserted", "deleted"])
def test_inserted_and_deleted_pages_display_wide_nullable_identifiers(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    repo = FakeRepository()
    counts = {kind: 2, "total": 2}
    repo.requests_by_id["req-1"] = make_request(
        **{**DRY_RUN_DONE, "DRY_RUN_COUNTS": counts, "DRY_RUN_DIFF_COUNTS": counts}
    )
    runner = Mock(spec=SqlRunner)
    runner.query.return_value = [
        {"ORDER_ID": 2**53 + 1, "CLIENT_ID": 10**37 + 1},
        {"ORDER_ID": None, "CLIENT_ID": None},
    ]
    monkeypatch.setattr(repo, "diff_columns", lambda _: ["ORDER_ID", "CLIENT_ID"])
    monkeypatch.setattr(
        repo, "diff_page", Repository(runner, '"APP"."CORE"', DEVELOPER_API).diff_page
    )
    download = Mock(wraps=st.download_button)
    monkeypatch.setattr(st, "download_button", download)
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    assert not at.exception
    frame = at.dataframe[0].value
    assert frame.at[0, "ORDER_ID"] == str(2**53 + 1) and frame.at[0, "CLIENT_ID"] == str(10**37 + 1)
    csv_rows = list(csv.DictReader(io.StringIO(download.call_args.args[1].decode())))
    assert csv_rows == [
        {"ORDER_ID": str(2**53 + 1), "CLIENT_ID": str(10**37 + 1)},
        {"ORDER_ID": "", "CLIENT_ID": ""},
    ]


def test_source_sample_preserves_exact_nullable_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepository()
    repo.requests_by_id["req-1"] = make_request()
    runner = Mock(spec=SqlRunner)
    runner.query.return_value = [
        {"SKU": 2**53 + 1, "PRICE": 10**37 + 1},
        {"SKU": None, "PRICE": None},
    ]
    real = Repository(runner, '"APP"."CORE"', DEVELOPER_API)
    monkeypatch.setattr(repo, "preview", real.preview)
    at = _app(repo, "req-1").switch_page("app_pages/workbench.py").run()
    at.toggle(key="preview_req-1").set_value(True).run()
    assert not at.exception
    sample = at.dataframe[-1].value
    assert sample.at[0, "SKU"] == str(2**53 + 1) and sample.at[0, "PRICE"] == str(10**37 + 1)
