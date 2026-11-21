"""Viewer identity must never silently fall back to the app owner in Snowflake."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from unittest.mock import Mock

import pytest
import streamlit as st
from fakes import FakeRepository
from streamlit.connections import SnowflakeConnection
from streamlit.runtime.caching import cache_resource_api
from streamlit.testing.v1 import AppTest

from auditable_crud.repository import ADMIN_API, DEVELOPER_API

APP = str(Path(__file__).parents[1] / "streamlit" / "developer_app" / "streamlit_app.py")


def _run_without_preset_actor(repo: FakeRepository) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["repository"] = repo
    return at.run()


def test_missing_viewer_identity_stops_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AC_LOCAL_DEV", raising=False)
    at = _run_without_preset_actor(FakeRepository(user="APP_OWNER_SESSION_USER"))
    assert not at.exception
    assert "Could not determine who you are" in at.error[0].value
    assert "actor" not in at.session_state
    assert not at.button  # no actions offered


def test_local_dev_may_use_the_session_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC_LOCAL_DEV", "1")
    at = _run_without_preset_actor(FakeRepository(user="LOCAL_DEV_USER"))
    assert not at.exception
    assert at.session_state["actor"] == "LOCAL_DEV_USER"


@pytest.mark.parametrize(
    ("app", "procedure"), [("developer_app", DEVELOPER_API), ("admin_app", ADMIN_API)]
)
def test_normal_entrypoint_binds_its_own_api_and_the_snowsight_viewer(
    monkeypatch: pytest.MonkeyPatch, app: str, procedure: str
) -> None:
    session = Mock(session_id=4512)
    session.get_current_database.return_value = '"APP"'
    session.sql.return_value.collect.return_value = []
    connection = Mock()
    connection.session.return_value = session
    monkeypatch.setattr(st, "connection", Mock(return_value=connection))
    monkeypatch.setattr(st, "user", {"user_name": "SIGNED_IN_VIEWER"})
    monkeypatch.setenv("AC_LOCAL_DEV", "1")
    app_file = Path(APP).parents[1] / app / "streamlit_app.py"
    at = AppTest.from_file(app_file, default_timeout=15).run()
    assert not at.exception
    assert at.session_state["actor"] == "SIGNED_IN_VIEWER"
    assert any(
        block.value.splitlines()
        == [
            f"Streamlit {version('streamlit')}",
            f"Pillow {version('Pillow')}",
            "Snowflake session 4512",
        ]
        for block in at.text
    )
    assert all("CURRENT_USER" not in call.args[0] for call in session.sql.call_args_list)
    result = Mock()
    result.as_dict.return_value = {"RESPONSE": {"ok": True, "result": {"total": 0}}}
    session.sql.return_value.collect.return_value = [result]
    assert at.session_state["repository"].call(
        "drift", actor="SIGNED_IN_VIEWER", request_id="r"
    ) == {"total": 0}
    assert session.sql.call_args.args == (f'CALL "APP"."CORE".{procedure}(?, PARSE_JSON(?))',)


def test_viewer_reconnect_replaces_only_its_closed_owner_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AppTest normally assigns every simulated browser the same session ID.
    monkeypatch.setattr(
        cache_resource_api, "get_session_id_or_throw", lambda: st.session_state["browser_session"]
    )
    sessions = [Mock(session_id=8123), Mock(session_id=9345), Mock(session_id=10567)]
    for session in sessions:
        session.get_current_database.return_value = '"APP"'
        session.sql.return_value.collect.return_value = []
    connections = [Mock(snowpark=session) for session in sessions]
    connect = Mock(side_effect=connections)
    monkeypatch.setattr(SnowflakeConnection, "_connect", connect)
    monkeypatch.setattr(SnowflakeConnection, "session", lambda owner: owner.raw_connection.snowpark)
    actors = ("FIRST_VIEWER", "SECOND_VIEWER")
    viewers = [AppTest.from_file(APP, default_timeout=15) for _ in actors]
    try:
        for viewer, actor in zip(viewers, actors, strict=True):
            viewer.user["user_name"] = actor
            viewer.session_state["browser_session"] = actor
            viewer.run()
            assert not viewer.exception
            assert viewer.session_state["actor"] == actor
        first, second = viewers
        first_repo = first.session_state["repository"]
        second_repo = second.session_state["repository"]
        first.run()
        assert not first.exception
        assert first.session_state["repository"] is first_repo
        assert [viewer.session_state["snowflake_session_id"] for viewer in viewers] == [8123, 9345]
        assert connect.call_count == 2
        for session in sessions:
            session.close.assert_not_called()
        first.session_state["request_id"] = "resume-this-request"
        cache_resource_api.clear_session_cache(actors[0])
        sessions[0].close.assert_called_once_with()
        sessions[0].sql.reset_mock()
        first.run()
        second.run()
        assert not first.exception and not second.exception
        assert first.session_state["actor"] == actors[0]
        assert first.session_state["request_id"] == "resume-this-request"
        assert first.session_state["repository"] is not first_repo
        assert second.session_state["repository"] is second_repo
        assert [viewer.session_state["snowflake_session_id"] for viewer in viewers] == [10567, 9345]
        sessions[0].sql.assert_not_called()
        sessions[2].sql.assert_called()
        sessions[1].close.assert_not_called()
        assert connect.call_count == 3
    finally:
        for actor in actors:
            cache_resource_api.clear_session_cache(actor)
    for session in sessions:
        session.close.assert_called_once_with()
