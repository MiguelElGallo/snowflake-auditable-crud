"""Managed-table configuration, read from the DCM-defined ``CORE.V_MANAGED_TABLES`` view."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from auditable_crud.naming import normalize_identifier
from auditable_crud.sql_guard import TableRef

DEFAULT_MAX_AFFECTED_ROWS = 10_000


def _split(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


@dataclass(frozen=True)
class TableConfig:
    """One table that may be changed through Auditable CRUD."""

    table_id: str
    source: TableRef
    clone_database: str
    clone_schema: str
    key_columns: tuple[str, ...]
    allowed_reads: frozenset[TableRef]
    max_affected_rows: int
    description: str
    ai_model: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TableConfig:
        """Build from a view row; validates every identifier it will put into SQL."""
        keys = tuple(normalize_identifier(k) for k in _split(row["KEY_COLUMNS"]))
        if not keys:
            raise ValueError(f"Table {row['TABLE_ID']} must declare at least one key column.")
        limit = row.get("MAX_AFFECTED_ROWS")
        max_rows = DEFAULT_MAX_AFFECTED_ROWS if limit is None else int(limit)
        if max_rows <= 0:
            raise ValueError("MAX_AFFECTED_ROWS must be positive.")
        return cls(
            table_id=normalize_identifier(str(row["TABLE_ID"])),
            source=TableRef.from_config(
                str(row["SOURCE_DATABASE"]), str(row["SOURCE_SCHEMA"]), str(row["SOURCE_TABLE"])
            ),
            clone_database=normalize_identifier(str(row["CLONE_DATABASE"])),
            clone_schema=normalize_identifier(str(row["CLONE_SCHEMA"])),
            key_columns=keys,
            allowed_reads=frozenset(
                TableRef.parse(t) for t in _split(row.get("ALLOWED_READ_TABLES"))
            ),
            max_affected_rows=max_rows,
            description=str(row.get("DESCRIPTION") or ""),
            ai_model=str(row.get("AI_MODEL") or "claude-sonnet-4-5"),
        )

    def sandbox_ref(self, name: str) -> TableRef:
        """Return a reference to a sandbox object (clone, stream, diff) for this table."""
        return TableRef(self.clone_database, self.clone_schema, name)
