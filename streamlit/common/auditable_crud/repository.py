"""Read/query layer used by both Streamlit apps.

All writes go through the app's owner's-rights procedure (``CORE.SP_DEV_API`` or
``CORE.SP_ADMIN_API``). Identifiers that reach SQL come from
``V_MANAGED_TABLES`` / ``CHANGE_REQUESTS`` (validated on the way in) and are
always quoted; user values are always bound parameters.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from auditable_crud.ai_sql import build_prompt, extract_sql
from auditable_crud.config import TableConfig
from auditable_crud.diff import (
    META_COLUMNS,
    ChangeKind,
    column_changes_sql,
    map_column_changes,
    page_sql,
    update_column_aliases,
)
from auditable_crud.drift import drift_rows_sql, overlap_column_name
from auditable_crud.naming import SandboxNames, information_schema, quote_identifier
from auditable_crud.runner import Row, SqlRunner, scalar
from auditable_crud.sql_guard import TableRef
from auditable_crud.store import decode_request_row

PAGE_SIZE = 200
REQUEST_LIST_COLUMNS = (
    "REQUEST_ID",
    "TABLE_ID",
    "STATUS",
    "REQUESTED_BY",
    "STATEMENT_KIND",
    "ROWS_AFFECTED",
    "CREATED_AT",
    "UPDATED_AT",
)


SandboxObject = Literal["clone", "stream", "diff", "reval_clone", "reval_stream", "reval_diff"]
DEVELOPER_API = "SP_DEV_API"
ADMIN_API = "SP_ADMIN_API"
API_PROCEDURES = frozenset({DEVELOPER_API, ADMIN_API})


class ApiError(RuntimeError):
    """An API procedure call returned ``ok = false``."""


@dataclass(frozen=True)
class RequestView:
    """The parts of a change-request row the UI needs, with typed accessors."""

    row: Row

    def __getitem__(self, key: str) -> Any:
        """Return a raw column value."""
        return self.row.get(key)

    @property
    def request_id(self) -> str:
        """Primary key."""
        return str(self.row["REQUEST_ID"])

    @property
    def status(self) -> str:
        """Current lifecycle status."""
        return str(self.row["STATUS"])

    @property
    def revision(self) -> int:
        """Return the revision a viewer saw, including pre-revision requests."""
        return int(self["REVISION"] or 0)

    @property
    def run_is_current(self) -> bool:
        """Whether the latest sandbox run finished, including pre-revision requests."""
        return self.revision == int(self["RUN_REVISION"] or 0)

    def counts(self, column: str) -> dict[str, int]:
        """Decode a VARIANT counts column (``{}`` when not yet set)."""
        value = self.row.get(column)
        if isinstance(value, str):
            value = json.loads(value)
        return {str(k): int(v) for k, v in (value or {}).items()}

    def sandbox(self, which: SandboxObject) -> TableRef:
        """Return a sandbox object of this request by ``SandboxNames`` attribute name."""
        name = SandboxNames(base=str(self.row["SANDBOX_BASE"])).published(self.row, which)
        return TableRef(str(self.row["CLONE_DATABASE"]), str(self.row["CLONE_SCHEMA"]), name)


class Repository:
    """Data access for the apps, bound to one deployment's ``CORE`` schema."""

    def __init__(self, runner: SqlRunner, core_schema: str, procedure: str) -> None:
        """``core_schema`` is the quoted ``"DB"."CORE"`` name; ``procedure`` the app's API.

        Each app may only call its own procedure (``SP_DEV_API`` or ``SP_ADMIN_API``).
        """
        if procedure not in API_PROCEDURES:
            raise ValueError(f"Unknown API procedure {procedure!r}")
        self._sql = runner
        self._core = core_schema
        self._procedure = procedure
        self._columns: dict[TableRef, list[tuple[str, str]]] = {}

    # ---------------------------------------------------------------- writes
    def call(self, op: str, **args: Any) -> dict[str, Any]:
        """Invoke this app's ``CORE.SP_*_API(op, args)``; return its result, raising on failure."""
        raw = scalar(
            self._sql,
            f"CALL {self._core}.{self._procedure}(?, PARSE_JSON(?))",
            [op, json.dumps(args)],
        )
        response = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if not response.get("ok"):
            raise ApiError(str(response.get("error") or "Unknown error"))
        return dict(response.get("result") or {})

    # ----------------------------------------------------------------- reads
    def current_user(self) -> str:
        """Snowflake user of the session (fallback when ``st.user`` is unavailable)."""
        return str(scalar(self._sql, "SELECT CURRENT_USER() AS U") or "")

    def tables(self) -> list[TableConfig]:
        """Read the current manifest, including deployments during this viewer's session."""
        rows = self._sql.query(f"SELECT * FROM {self._core}.V_MANAGED_TABLES ORDER BY TABLE_ID")
        return [TableConfig.from_row(r) for r in rows]

    def table_stats(self, cfg: TableConfig) -> dict[str, Any]:
        """Row count, last alteration, and Time Travel retention of a source table."""
        rows = self._sql.query(
            "SELECT ROW_COUNT, LAST_ALTERED, RETENTION_TIME "
            f"FROM {information_schema(cfg.source.database)}.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [cfg.source.schema, cfg.source.name],
        )
        return rows[0] if rows else {"ROW_COUNT": None, "LAST_ALTERED": None}

    def columns(self, cfg: TableConfig) -> list[tuple[str, str]]:
        """``(name, data_type)`` for each source column in ordinal order (cached)."""
        return self._column_metadata(cfg.source)

    def diff_columns(self, diff: TableRef) -> list[str]:
        """Return the captured columns even if the live table's schema changed."""
        return [name for name, _ in self._column_metadata(diff) if name not in META_COLUMNS]

    def _column_metadata(self, ref: TableRef, *, refresh: bool = False) -> list[tuple[str, str]]:
        if refresh or ref not in self._columns:
            rows = self._sql.query(
                "SELECT COLUMN_NAME, DATA_TYPE "
                f"FROM {information_schema(ref.database)}.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                [ref.schema, ref.name],
            )
            self._columns[ref] = [(str(r["COLUMN_NAME"]), str(r["DATA_TYPE"])) for r in rows]
        return self._columns[ref]

    def preview(self, cfg: TableConfig, limit: int = 100) -> pd.DataFrame:
        """First ``limit`` rows of the source table."""
        return pd.DataFrame(
            self._sql.query(f"SELECT * FROM {cfg.source.sql} LIMIT ?", [limit]), dtype=object
        )

    def requests(
        self, *, requested_by: str | None = None, statuses: Sequence[str] = ()
    ) -> pd.DataFrame:
        """Change requests, newest first, optionally filtered.

        The frame always has ``REQUEST_LIST_COLUMNS``, even when empty.
        """
        clauses, params = [], []
        if requested_by:
            clauses.append("UPPER(REQUESTED_BY) = UPPER(?)")
            params.append(requested_by)
        if statuses:
            clauses.append(f"STATUS IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._sql.query(
            "SELECT REQUEST_ID, TABLE_ID, STATUS, REQUESTED_BY, STATEMENT_KIND, "
            "DRY_RUN_COUNTS:total::INT AS ROWS_AFFECTED, CREATED_AT, UPDATED_AT "
            f"FROM {self._core}.CHANGE_REQUESTS{where} ORDER BY CREATED_AT DESC",
            params,
        )
        return pd.DataFrame(rows, columns=list(REQUEST_LIST_COLUMNS))

    def request(self, request_id: str) -> RequestView:
        """One change request."""
        rows = self._sql.query(
            f"SELECT * FROM {self._core}.CHANGE_REQUESTS WHERE REQUEST_ID = ?", [request_id]
        )
        if not rows:
            raise ApiError(f"Change request {request_id} not found.")
        return RequestView(decode_request_row(rows[0]))

    def audit(self, request_id: str) -> pd.DataFrame:
        """Audit trail of a request, oldest first."""
        return pd.DataFrame(
            self._sql.query(
                f"SELECT EVENT_AT, EVENT, ACTOR, DETAILS FROM {self._core}.AUDIT_LOG "
                "WHERE REQUEST_ID = ? ORDER BY EVENT_AT, EVENT_ID",
                [request_id],
            )
        )

    def diff_page(
        self, diff: TableRef, kind: ChangeKind, columns: Sequence[str], page: int = 0
    ) -> pd.DataFrame:
        """One page of inserted, deleted or (before/after) updated rows."""
        rows = self._sql.query(
            page_sql(diff.sql, kind, columns), [PAGE_SIZE, max(page, 0) * PAGE_SIZE]
        )
        if kind is ChangeKind.UPDATED:
            labels = {}
            for column, before, after in update_column_aliases(columns):
                labels[before] = f"{column} (before)"
                labels[after] = f"{column} (after)"
            # Keep nullable Snowflake integers exact until display and CSV export.
            return pd.DataFrame(rows, columns=list(labels), dtype=object).rename(columns=labels)
        return pd.DataFrame(rows, columns=list(columns), dtype=object)

    def column_changes(self, diff: TableRef, columns: Sequence[str]) -> dict[str, int]:
        """Per-column count of updated rows whose value changed."""
        rows = self._sql.query(column_changes_sql(diff.sql, columns))
        return map_column_changes(rows[0] if rows else {}, columns)

    def drift_rows(
        self,
        cfg: TableConfig,
        baseline_ns: int,
        diff: TableRef,
        limit: int = PAGE_SIZE,
        *,
        end_ns: int | None = None,
    ) -> pd.DataFrame:
        """Return changed source rows and name the overlap flag in ``attrs["overlap_column"]``."""
        columns = [name for name, _ in self._column_metadata(cfg.source, refresh=True)]
        overlap = overlap_column_name(columns)
        sql = drift_rows_sql(
            cfg.source.sql, baseline_ns, diff.sql, cfg.key_columns, columns, end_ns=end_ns
        )
        frame = pd.DataFrame(
            self._sql.query(sql, [limit]),
            columns=["AC_ACTION", "AC_ISUPDATE", overlap, *columns],
            dtype=object,
        )
        frame.attrs["overlap_column"] = overlap
        return frame

    def generate_sql(self, cfg: TableConfig, request: str) -> str:
        """Ask Cortex for a DML statement implementing ``request``."""
        columns = self.columns(cfg)
        sample = self._sql.query(f"SELECT * FROM {cfg.source.sql} SAMPLE (5 ROWS)")
        prompt = build_prompt(cfg.source, columns, sample, request, cfg.key_columns)
        answer = scalar(self._sql, "SELECT AI_COMPLETE(?, ?) AS ANSWER", [cfg.ai_model, prompt])
        return extract_sql(str(answer or ""))


def core_schema_for(database: str) -> str:
    """Quoted ``"DB"."CORE"`` for the app's deployment database."""
    return f"{quote_identifier(database)}.{quote_identifier('CORE')}"
