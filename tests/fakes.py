"""Test doubles shared by the engine and procedure-bundle tests."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from auditable_crud.config import TableConfig
from auditable_crud.repository import ApiError, RequestView
from auditable_crud.runner import Row
from auditable_crud.state import WorkflowError

CONFIG: Row = {
    "TABLE_ID": "PRICING_CONFIG",
    "SOURCE_DATABASE": "APP",
    "SOURCE_SCHEMA": "DEMO",
    "SOURCE_TABLE": "PRICING_CONFIG",
    "CLONE_DATABASE": "APP",
    "CLONE_SCHEMA": "SANDBOX",
    "KEY_COLUMNS": "SKU",
    "ALLOWED_READ_TABLES": "",
    "MAX_AFFECTED_ROWS": 100,
}
SOURCE = '"APP"."DEMO"."PRICING_CONFIG"'
UPDATE_SQL = "UPDATE pricing_config SET price = price * 1.1 WHERE region = 'EU'"

Responder = Callable[[str], list[Row]]
COLUMNS: list[Row] = [{"COLUMN_NAME": c} for c in ("SKU", "PRICE", "REGION")]
# Types the Snowflake connector binds itself; anything else (Enum, dict, ...) fails there.
BINDABLE = (str, int, float, bool, bytes, type(None))


def check_bindable(params: Sequence[Any]) -> None:
    """Fail like Snowpark does on parameters it cannot bind (an Enum hid a real bug)."""
    for value in params:
        if not isinstance(value, BINDABLE) or type(value).__module__ != "builtins":
            raise TypeError(f"cannot bind {type(value).__name__} parameter {value!r}")


class FakeSnowflake:
    """Scripted :class:`SqlRunner`: the first matching regex decides the result rows."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.dml_result: Row = {"number of rows updated": 3}
        self.drift: Row = {"INSERTED": 0, "UPDATED": 0, "DELETED": 0}
        self.overlap = 0
        self.fail_dml_on: str | None = None
        self.fail_on: str | None = None  # any statement equal to this raises
        self._ns = itertools.count(1_700_000_000_000_000_000, 1_000)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        check_bindable(params)
        self.log.append(sql)
        rules: list[tuple[str, Responder]] = [
            (r"COALESCE\(LAST_DDL, CREATED\)", lambda _: [{"DDL_NS": 1}]),
            (r"EPOCH_NANOSECOND", lambda _: [{"NS": next(self._ns)}]),
            (r"LAST_QUERY_ID", lambda _: [{"QID": "01-query"}]),
            (r"INFORMATION_SCHEMA\.COLUMNS", lambda _: COLUMNS),
            (r"SELECT RETENTION_TIME", lambda _: [{"RETENTION_TIME": 1}]),
            (r"HASH_AGG", lambda _: [{"FINGERPRINT": 42}]),
            (r"OVERLAPPING_KEYS", lambda _: [{"OVERLAPPING_KEYS": self.overlap}]),
            (r"CHANGES\(INFORMATION", lambda _: [self.drift]),
            (r"COUNT_IF\(AC_ACTION", lambda _: [{"INSERTED": 0, "UPDATED": 3, "DELETED": 0}]),
            (r"^(UPDATE|INSERT|DELETE|MERGE) ", self._dml),
        ]
        if self.fail_on and sql == self.fail_on:
            raise RuntimeError(f"{sql} failed")
        for pattern, respond in rules:
            if re.search(pattern, sql):
                return respond(sql)
        return []

    def _dml(self, sql: str) -> list[Row]:
        if self.fail_dml_on and self.fail_dml_on in sql:
            raise RuntimeError("Numeric value 'abc' is not recognized")
        return [self.dml_result]

    def dml_statements(self) -> list[str]:
        return [s for s in self.log if re.match(r"^(UPDATE|INSERT|DELETE|MERGE) ", s)]

    def query_dml(self, sql: str) -> list[Row]:
        return self.query(sql)


class MemoryStore:
    """In-memory :class:`RequestStore` with the same compare-and-set semantics."""

    def __init__(self, runner: FakeSnowflake | None = None) -> None:
        self.requests: dict[str, Row] = {}
        self.events: list[tuple[str, str, str]] = []
        self.runner = runner
        self._snapshot: tuple[dict[str, Row], list[tuple[str, str, str]]] | None = None

    def load_config(self, table_id: str) -> Row:
        if table_id != CONFIG["TABLE_ID"]:
            raise WorkflowError("not configured")
        return dict(CONFIG)

    def get(self, request_id: str) -> Row:
        return dict(self.requests[request_id])

    def insert(self, values: Mapping[str, Any]) -> None:
        self.requests[str(values["REQUEST_ID"])] = dict(values)

    def update(
        self,
        request_id: str,
        values: Mapping[str, Any],
        expected_status: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        row = self.requests.get(request_id)
        if row is None:
            return 0  # as the real UPDATE matching no row
        if expected_status is not None and row["STATUS"] != expected_status:
            return 0
        if expected_revision is not None and int(row.get("REVISION") or 0) != expected_revision:
            return 0
        row.update(values)
        return 1

    def audit(self, request_id: str, event: str, actor: str, details: Mapping[str, Any]) -> None:
        self.events.append((request_id, event, actor))

    def cleanup_candidates(self, retention_days: int, request_id: str | None = None) -> list[Row]:
        final = {"EXECUTED", "REJECTED", "FAILED", "CANCELLED"}  # as store.cleanup_candidates
        return [
            dict(r)
            for r in self.requests.values()
            if r["STATUS"] in final
            and not r.get("SANDBOX_DROPPED_AT")
            and (request_id is None or r["REQUEST_ID"] == request_id)
        ]

    def idle_drafts(self, days: int, request_id: str | None = None) -> list[Row]:
        return [
            dict(r)
            for r in self.requests.values()
            if r["STATUS"] in {"DRAFT", "DRY_RUN_OK"}
            and (request_id is None or r["REQUEST_ID"] == request_id)
            and r.get("IDLE_DAYS", 0) > days
            and (
                r.get("REVISION") == r.get("RUN_REVISION")
                or r.get("LAST_ERROR")
                or r["IDLE_DAYS"] > 7
            )
        ]

    def begin(self) -> None:
        assert self._snapshot is None
        if self.runner:
            self.runner.query("BEGIN TRANSACTION")
        self._snapshot = {k: dict(v) for k, v in self.requests.items()}, list(self.events)

    def commit(self) -> None:
        assert self._snapshot is not None
        if self.runner:
            self.runner.query("COMMIT")
        self._snapshot = None

    def rollback(self) -> None:
        assert self._snapshot is not None
        if self.runner:
            self.runner.query("ROLLBACK")
        self.requests, self.events = self._snapshot
        self._snapshot = None


class FakeRepository:
    """Duck-typed stand-in for :class:`auditable_crud.repository.Repository` in AppTests.

    Records every ``SP_API`` call in ``calls`` and answers reads from ``requests_by_id``.
    """

    def __init__(self, user: str = "dev") -> None:
        self.user = user
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.configs = [TableConfig.from_row(CONFIG), TableConfig.from_row(FLAGS_CONFIG)]
        self.requests_by_id: dict[str, Row] = {}

    # writes
    def call(self, op: str, **args: Any) -> dict[str, Any]:
        self.calls.append((op, args))
        if op in self.responses and "error" in self.responses[op]:
            raise ApiError(self.responses[op]["error"])
        return self.responses.get(op, {"request_id": args.get("request_id", "new-req")})

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    # reads
    def current_user(self) -> str:
        return self.user

    def tables(self) -> list[Any]:
        return self.configs

    def table_stats(self, cfg: Any) -> Row:
        return {"ROW_COUNT": 1000, "LAST_ALTERED": None}

    def columns(self, cfg: Any) -> list[tuple[str, str]]:
        return [("SKU", "TEXT"), ("REGION", "TEXT"), ("PRICE", "NUMBER")]

    def diff_columns(self, diff: Any) -> list[str]:
        return [name for name, _ in self.columns(diff)]

    def preview(self, cfg: Any, limit: int = 100) -> Any:
        return pd.DataFrame([{"SKU": "A", "REGION": "EU", "PRICE": 1}])

    def requests(self, *, requested_by: str | None = None, statuses: Sequence[str] = ()) -> Any:
        rows = [
            {k: r[k] for k in ("REQUEST_ID", "TABLE_ID", "STATUS", "REQUESTED_BY")}
            for r in self.requests_by_id.values()
            if (not requested_by or r["REQUESTED_BY"] == requested_by)
            and (not statuses or r["STATUS"] in statuses)
        ]
        return pd.DataFrame(rows, columns=["REQUEST_ID", "TABLE_ID", "STATUS", "REQUESTED_BY"])

    def request(self, request_id: str) -> Any:
        if request_id not in self.requests_by_id:
            raise ApiError(f"Request {request_id} not found.")  # as the real repository
        return RequestView(self.requests_by_id[request_id])

    def audit(self, request_id: str) -> Any:
        return pd.DataFrame(
            [{"EVENT_AT": None, "EVENT": "CREATED", "ACTOR": "dev", "DETAILS": "{}"}]
        )

    def diff_page(self, diff: Any, kind: Any, columns: Sequence[str], page: int = 0) -> Any:
        if str(kind) == "updated":
            return pd.DataFrame(
                [{"SKU (before)": "A", "SKU (after)": "A", "PRICE (before)": 1, "PRICE (after)": 2}]
            )
        return pd.DataFrame(columns=list(columns))

    def column_changes(self, diff: Any, columns: Sequence[str]) -> dict[str, int]:
        return dict.fromkeys(columns, 1)

    def drift_rows(
        self, cfg: Any, baseline_ns: int, diff: Any, limit: int = 200, *, end_ns: int | None = None
    ) -> Any:
        return pd.DataFrame([{"AC_ACTION": "INSERT", "AC_ISUPDATE": True, "AC_OVERLAPS": True}])

    def generate_sql(self, cfg: Any, request: str) -> str:
        return f"UPDATE {cfg.source.name} SET price = price * 1.05 WHERE region = 'EU'"


FLAGS_CONFIG: Row = {
    **CONFIG,
    "TABLE_ID": "FEATURE_FLAGS",
    "SOURCE_TABLE": "FEATURE_FLAGS",
    "KEY_COLUMNS": "FLAG_KEY,ENVIRONMENT",
}


def make_request(**overrides: Any) -> Row:
    """A CHANGE_REQUESTS row as returned by the repository."""
    row: Row = {
        "REQUEST_ID": "req-1",
        "TABLE_ID": "PRICING_CONFIG",
        "SOURCE_FQN": "APP.DEMO.PRICING_CONFIG",
        "STATUS": "DRAFT",
        "REQUESTED_BY": "dev",
        "CLONE_DATABASE": "APP",
        "CLONE_SCHEMA": "SANDBOX",
        "SANDBOX_BASE": "PRICING_CONFIG__CR_20261121130509_DEV_REQ1",
        "SNAPSHOT_NS": 1_700_000_000_000_000_000,
        "SQL_TEXT": None,
        "CANONICAL_SQL": None,
        "STATEMENT_KIND": None,
        "DRY_RUN_COUNTS": None,
        "DRY_RUN_DIFF_COUNTS": None,
        "DRY_RUN_ELAPSED_MS": None,
        "DRY_RUN_QUERY_ID": None,
        "REVAL_COUNTS": None,
        "REVAL_SNAPSHOT_NS": None,
        "REVAL_MATCHES": None,
        "EXEC_COUNTS": None,
        "REVIEW_COMMENT": None,
        "REVIEWED_BY": None,
        "SUBMIT_NOTE": None,
        "LAST_ERROR": None,
        "TOLERANCE_PCT": None,
    }
    row.update(overrides)
    return row


DRY_RUN_DONE: Row = {
    "STATUS": "DRY_RUN_OK",
    "SQL_TEXT": UPDATE_SQL,
    "CANONICAL_SQL": UPDATE_SQL,
    "STATEMENT_KIND": "UPDATE",
    "DRY_RUN_COUNTS": {"inserted": 0, "updated": 3, "deleted": 0, "total": 3},
    "DRY_RUN_DIFF_COUNTS": {"inserted": 0, "updated": 3, "deleted": 0, "total": 3},
    "DRY_RUN_ELAPSED_MS": 412,
    "DRY_RUN_QUERY_ID": "01-q",
}
