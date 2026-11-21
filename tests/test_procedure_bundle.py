"""Exercise the generated developer and admin handlers as independent Snowflake modules."""

from __future__ import annotations

import sys
import types
from collections.abc import Sequence
from typing import Any

import pytest
from fakes import SOURCE, UPDATE_SQL, FakeSnowflake, MemoryStore

from auditable_crud.repository import ADMIN_API, DEVELOPER_API
from auditable_crud.runner import Row
from scripts.build_procedure import OUTPUT, BundleError, _strip_module, render_definition


def _procedure_namespace(procedure: str, app_db: str = "APP") -> dict[str, Any]:
    """Load the procedure body as a module, the way Snowflake loads an inline handler."""
    definition = OUTPUT.read_text("utf-8").replace("{{ app_db }}", app_db)
    (statement,) = [
        part
        for part in definition.split("DEFINE PROCEDURE ")
        if part.startswith(f"{app_db}.CORE.{procedure}(")
    ]
    body = statement.split("$$")[1]
    module = types.ModuleType(f"bundle_{app_db}_{procedure}")
    sys.modules[module.__name__] = module  # dataclasses resolve annotations via sys.modules
    exec(compile(body, f"<{procedure}>", "exec"), module.__dict__)  # noqa: S102
    return module.__dict__


def test_committed_procedure_is_up_to_date() -> None:
    assert OUTPUT.read_text("utf-8") == render_definition(), (
        "procedures.sql is stale: run `uv run python scripts/build_procedure.py`"
    )


def test_bundled_handlers_run_the_full_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, admin = _procedure_namespace(DEVELOPER_API), _procedure_namespace(ADMIN_API)
    sf, store = FakeSnowflake(), MemoryStore()
    for namespace in (dev, admin):
        monkeypatch.setitem(namespace, "SnowparkRunner", lambda _session: sf)
        monkeypatch.setitem(namespace, "SqlRequestStore", lambda *_args: store)

    def call(namespace: dict[str, Any], op: str, actor: str, **args: Any) -> dict[str, Any]:
        response = namespace["handler"](None, op, {"actor": actor, **args})
        assert response["ok"], response
        return dict(response["result"])

    rid = call(dev, "create_request", "dev", table_id="PRICING_CONFIG")["request_id"]
    call(dev, "dry_run", "dev", request_id=rid, sql=UPDATE_SQL)
    call(dev, "submit", "dev", request_id=rid)
    call(admin, "review", "admin", request_id=rid, decision="APPROVE", comment="fine")
    assert call(admin, "execute", "admin", request_id=rid)["status"] == "EXECUTED"
    assert sf.dml_statements()[-1].startswith(f"UPDATE {SOURCE}")


class _Row:
    def __init__(self, data: Row) -> None:
        self._data = data

    def as_dict(self) -> Row:
        return self._data


class _Session:
    """Minimal stand-in for snowflake.snowpark.Session used by SnowparkRunner."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Sequence[Any] | None]] = []

    def sql(self, query: str, params: Sequence[Any] | None = None) -> _Session:
        self.calls.append((query, params))
        return self

    def collect(self) -> list[_Row]:
        return [_Row({"STATUS": "DRAFT"})]


@pytest.mark.parametrize("procedure", [DEVELOPER_API, ADMIN_API])
def test_handler_uses_rendered_core_schema_and_never_raises(procedure: str) -> None:
    ns = _procedure_namespace(procedure, "MY_APP_DB")
    session = _Session()
    response = ns["handler"](session, "drift", {"actor": "a", "request_id": "r1"})
    assert response["ok"] is False  # the fake row lacks snapshot columns
    assert session.calls[0] == (
        'SELECT * FROM "MY_APP_DB"."CORE".CHANGE_REQUESTS WHERE REQUEST_ID = ?',
        ["r1"],
    )


@pytest.mark.parametrize(("procedure", "op"), [(DEVELOPER_API, "review"), (ADMIN_API, "dry_run")])
def test_handler_refuses_operations_from_the_other_app(procedure: str, op: str) -> None:
    session = _Session()
    response = _procedure_namespace(procedure)["handler"](session, op, {"actor": "a"})
    assert response["ok"] is False and "not available" in response["error"]
    assert session.calls == []


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("from auditable_crud.naming import fqn as f\n", "aliased"),
        ("from os import *\n", "star"),
        ("def f():\n    from auditable_crud.naming import fqn\n", "module level"),
    ],
)
def test_bundler_rejects_unbundleable_imports(source: str, error: str) -> None:
    with pytest.raises(BundleError, match=error):
        _strip_module("bad", source)
