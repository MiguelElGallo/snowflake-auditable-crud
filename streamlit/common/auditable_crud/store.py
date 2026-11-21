"""Persistence for change requests and the audit log (``CORE`` schema tables)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol

from auditable_crud.diff import DiffCounts
from auditable_crud.naming import normalize_identifier
from auditable_crud.runner import Row, SqlRunner
from auditable_crud.state import WorkflowError

VARIANT_COLUMNS = frozenset(
    {"DRY_RUN_COUNTS", "DRY_RUN_DIFF_COUNTS", "REVAL_COUNTS", "EXEC_COUNTS"}
)
_COLUMN_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class _Now:
    """Sentinel meaning "set this column to CURRENT_TIMESTAMP() on the server"."""

    def __repr__(self) -> str:
        return "NOW"


NOW: Any = _Now()


class RequestStore(Protocol):
    """Storage operations the workflow engine needs."""

    def load_config(self, table_id: str) -> Row:
        """Return the ``V_MANAGED_TABLES`` row for ``table_id``."""
        ...

    def get(self, request_id: str) -> Row:
        """Return one change request (VARIANT columns decoded)."""
        ...

    def insert(self, values: Mapping[str, Any]) -> None:
        """Insert a new change request."""
        ...

    def update(
        self,
        request_id: str,
        values: Mapping[str, Any],
        expected_status: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        """Update a request; with ``expected_*`` only if it still has that status / revision.

        Returns the number of rows updated (0 means a concurrent change won).
        """
        ...

    def audit(self, request_id: str, event: str, actor: str, details: Mapping[str, Any]) -> None:
        """Append an immutable audit event."""
        ...

    def cleanup_candidates(self, retention_days: int, request_id: str | None = None) -> list[Row]:
        """Return final requests older than ``retention_days`` whose sandbox still exists."""
        ...

    def idle_drafts(self, days: int, request_id: str | None = None) -> list[Row]:
        """Return idle editable requests without an active sandbox operation."""
        ...

    def begin(self) -> None:
        """Start a transaction."""
        ...

    def commit(self) -> None:
        """Commit the current transaction."""
        ...

    def rollback(self) -> None:
        """Roll back the current transaction."""
        ...


def _check_column(name: str) -> str:
    if not _COLUMN_NAME.match(name):
        raise ValueError(f"Invalid column name {name!r}")
    return name


def decode_request_row(row: Row) -> Row:
    """Decode stored counts and empty machine-generated fields from earlier handlers."""
    decoded = dict(row)
    # Earlier warehouse handlers sometimes bound Python None as the text 'None'.
    # User-authored prompts, comments and notes must keep their exact text.
    for column in ("DRY_RUN_FINGERPRINT", "REVAL_FINGERPRINT", "APPROVED_SHA256"):
        if decoded.get(column) == "None":
            decoded[column] = None
    if decoded.get("LAST_ERROR") == "None" and decoded.get("STATUS") != "FAILED":
        decoded["LAST_ERROR"] = None
    for column in VARIANT_COLUMNS & decoded.keys():
        value = decoded[column]
        if isinstance(value, str):
            decoded[column] = json.loads(value)
    return decoded


def _bindable(value: Any) -> Any:
    """Convert values Snowpark cannot bind (e.g. ``StrEnum`` members) to plain types."""
    if isinstance(value, Enum):
        return value.value
    return value


def _assignments(values: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    """Render ``COL = ?`` assignments, JSON-encoding dict/list values for VARIANT columns."""
    parts: list[str] = []
    params: list[Any] = []
    for column, value in values.items():
        name = _check_column(column)
        if value is NOW:
            parts.append(f"{name} = CURRENT_TIMESTAMP()")
        elif value is None:
            parts.append(f"{name} = NULL")
        elif isinstance(value, dict | list):
            parts.append(f"{name} = PARSE_JSON(?)")
            params.append(json.dumps(value))
        else:
            parts.append(f"{name} = ?")
            params.append(_bindable(value))
    return parts, params


class SqlRequestStore:
    """:class:`RequestStore` implementation over the DCM-defined CORE tables."""

    def __init__(self, runner: SqlRunner, core_schema: str) -> None:
        """``core_schema`` is the quoted ``"DB"."CORE"`` name (from deployment config)."""
        self._runner = runner
        self._core = core_schema

    def load_config(self, table_id: str) -> Row:
        """See :meth:`RequestStore.load_config`."""
        rows = self._runner.query(
            f"SELECT * FROM {self._core}.V_MANAGED_TABLES WHERE UPPER(TRIM(TABLE_ID)) = ?",
            [normalize_identifier(table_id)],
        )
        if not rows:
            raise WorkflowError(f"Table {table_id!r} is not configured for change requests.")
        if len(rows) != 1:
            raise WorkflowError(f"Table ID {table_id!r} occurs more than once in the manifest.")
        return rows[0]

    def get(self, request_id: str) -> Row:
        """See :meth:`RequestStore.get`."""
        rows = self._runner.query(
            f"SELECT * FROM {self._core}.CHANGE_REQUESTS WHERE REQUEST_ID = ?", [request_id]
        )
        if not rows:
            raise WorkflowError(f"Change request {request_id!r} does not exist.")
        return decode_request_row(rows[0])

    def insert(self, values: Mapping[str, Any]) -> None:
        """See :meth:`RequestStore.insert`."""
        parts, params = _assignments(values)
        columns = ", ".join(p.split(" = ", 1)[0] for p in parts)
        exprs = ", ".join(p.split(" = ", 1)[1] for p in parts)
        self._runner.query(
            f"INSERT INTO {self._core}.CHANGE_REQUESTS ({columns}) SELECT {exprs}", params
        )

    def update(
        self,
        request_id: str,
        values: Mapping[str, Any],
        expected_status: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        """See :meth:`RequestStore.update`."""
        parts, params = _assignments({**values, "UPDATED_AT": NOW})
        sql = f"UPDATE {self._core}.CHANGE_REQUESTS SET {', '.join(parts)} WHERE REQUEST_ID = ?"
        params.append(request_id)
        if expected_status is not None:
            sql += " AND STATUS = ?"
            params.append(_bindable(expected_status))
        if expected_revision is not None:
            sql += " AND COALESCE(REVISION, 0) = ?"  # rows from before REVISION existed are 0
            params.append(int(expected_revision))
        rows = self._runner.query(sql, params)
        return DiffCounts.from_dml_result(rows[0]).updated if rows else 0

    def begin(self) -> None:
        """Start a transaction (status changes and their audit events commit together)."""
        self._runner.query("BEGIN TRANSACTION")

    def commit(self) -> None:
        """Commit the current transaction."""
        self._runner.query("COMMIT")

    def rollback(self) -> None:
        """Roll back the current transaction."""
        self._runner.query("ROLLBACK")

    def audit(self, request_id: str, event: str, actor: str, details: Mapping[str, Any]) -> None:
        """See :meth:`RequestStore.audit`."""
        self._runner.query(
            f"INSERT INTO {self._core}.AUDIT_LOG (REQUEST_ID, EVENT, ACTOR, DETAILS) "
            "SELECT ?, ?, ?, PARSE_JSON(?)",
            [request_id, event, actor, json.dumps(dict(details), default=str)],
        )

    def cleanup_candidates(self, retention_days: int, request_id: str | None = None) -> list[Row]:
        """See :meth:`RequestStore.cleanup_candidates`."""
        sql = (
            f"SELECT * FROM {self._core}.CHANGE_REQUESTS WHERE SANDBOX_DROPPED_AT IS NULL "
            "AND STATUS IN ('EXECUTED', 'FAILED', 'REJECTED', 'CANCELLED')"
        )
        params: list[Any] = []
        if request_id is not None:
            sql += " AND REQUEST_ID = ?"
            params.append(request_id)
        if retention_days:
            sql += " AND UPDATED_AT < DATEADD(DAY, -?, CURRENT_TIMESTAMP())"
            params.append(retention_days)
        return self._runner.query(sql, params)

    def idle_drafts(self, days: int, request_id: str | None = None) -> list[Row]:
        """See :meth:`RequestStore.idle_drafts`."""
        sql = (
            f"SELECT * FROM {self._core}.CHANGE_REQUESTS WHERE STATUS IN ('DRAFT', 'DRY_RUN_OK') "
            # Snowflake caps the total time of a CALL at seven days. A claim older
            # than that can no longer represent a running procedure, even if it
            # was interrupted before its handler could persist an error.
            "AND (COALESCE(REVISION, 0) = COALESCE(RUN_REVISION, 0) "
            "OR NULLIF(LAST_ERROR, 'None') IS NOT NULL "
            "OR UPDATED_AT < DATEADD(DAY, -7, CURRENT_TIMESTAMP())) "
            "AND UPDATED_AT < DATEADD(DAY, -?, CURRENT_TIMESTAMP())"
        )
        params: list[Any] = [days]
        if request_id is not None:
            sql += " AND REQUEST_ID = ?"
            params.append(request_id)
        return self._runner.query(sql, params)
