"""Turning a stream on the clone into a precise, browsable change set.

The stream is created on the clone *before* the DML runs, so afterwards it
holds exactly the rows the statement touched. Its contents are copied into a
"diff" table (``METADATA$`` columns renamed to ``AC_*``) so that the result is
stable and can be browsed by developer and admin long after the dry run.

An UPDATE shows up as a DELETE/INSERT pair sharing ``METADATA$ROW_ID`` with
``METADATA$ISUPDATE = TRUE``: the DELETE side is the row before, the INSERT
side the row after.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from auditable_crud.naming import quote_identifier

META_COLUMNS = ("AC_ACTION", "AC_ISUPDATE", "AC_ROW_ID")


class ChangeKind(StrEnum):
    """The three views of a change set."""

    INSERTED = "inserted"
    UPDATED = "updated"
    DELETED = "deleted"


@dataclass(frozen=True)
class DiffCounts:
    """Number of rows inserted / updated / deleted by a statement."""

    inserted: int = 0
    updated: int = 0
    deleted: int = 0

    @property
    def total(self) -> int:
        """Rows affected in any way."""
        return self.inserted + self.updated + self.deleted

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> DiffCounts:
        """Read the ``INSERTED``/``UPDATED``/``DELETED`` columns of a counts query."""
        return cls(
            inserted=int(row.get("INSERTED") or 0),
            updated=int(row.get("UPDATED") or 0),
            deleted=int(row.get("DELETED") or 0),
        )

    @classmethod
    def from_dml_result(cls, row: Mapping[str, Any]) -> DiffCounts:
        """Parse the row Snowflake returns for a DML statement.

        Keys look like ``number of rows updated``; MERGE returns several. UPDATE
        also returns ``number of multi-joined rows updated``, a subset of the
        updated rows, which must not be counted twice.
        """
        labels = {label.lower(): value for label, value in row.items()}
        counts = {
            kind: int(labels[f"number of rows {kind}"])
            for kind in ("inserted", "updated", "deleted")
            if f"number of rows {kind}" in labels
        }
        if not counts:
            raise ValueError("Snowflake did not return recognized DML row counts.")
        return cls(**counts)

    def as_dict(self) -> dict[str, int]:
        """JSON-friendly form including the total."""
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "total": self.total,
        }


def materialize_sql(diff_sql: str, stream_sql: str) -> str:
    """CTAS copying the stream (with renamed metadata columns) into the diff table."""
    return (
        f"CREATE OR REPLACE TABLE {diff_sql} AS SELECT * EXCLUDE "
        "(METADATA$ACTION, METADATA$ISUPDATE, METADATA$ROW_ID), "
        "METADATA$ACTION AS AC_ACTION, METADATA$ISUPDATE AS AC_ISUPDATE, "
        f"METADATA$ROW_ID AS AC_ROW_ID FROM {stream_sql}"
    )


def counts_sql(diff_sql: str) -> str:
    """Count inserted / updated / deleted rows in a diff table."""
    return (
        "SELECT COUNT_IF(AC_ACTION = 'INSERT' AND NOT AC_ISUPDATE) AS INSERTED, "
        "COUNT_IF(AC_ACTION = 'INSERT' AND AC_ISUPDATE) AS UPDATED, "
        "COUNT_IF(AC_ACTION = 'DELETE' AND NOT AC_ISUPDATE) AS DELETED "
        f"FROM {diff_sql}"
    )


def fingerprint_sql(diff_sql: str, columns: Sequence[str]) -> str:
    """Order-independent hash of the change set's content.

    ``AC_ROW_ID`` is excluded on purpose: two clones taken at different times
    may assign different row ids to the same logical row.
    """
    cols = ", ".join(quote_identifier(c) for c in columns)
    return f"SELECT HASH_AGG(AC_ACTION, AC_ISUPDATE, {cols}) AS FINGERPRINT FROM {diff_sql}"


def update_column_aliases(columns: Sequence[str]) -> Iterator[tuple[str, str, str]]:
    """Yield the source column and its short, unique before and after SQL aliases."""
    for position, column in enumerate(columns):
        yield column, f"B{position}", f"A{position}"


def _update_join(diff_sql: str, columns: Sequence[str]) -> str:
    # Qualify and shorten the names on each side before joining. With two
    # 255-character fields, Snowflake can exceed the limit adding an internal
    # suffix even when the outer SELECT already has short aliases.
    before_fields = ["d.AC_ROW_ID AS B_ROW_ID"]
    after_fields = ["d.AC_ROW_ID AS A_ROW_ID"]
    for column, before, after in update_column_aliases(columns):
        source = f"d.{quote_identifier(column)}"
        before_fields.append(f"{source} AS {before}")
        after_fields.append(f"{source} AS {after}")
    return (
        f"FROM (SELECT {', '.join(before_fields)} FROM {diff_sql} d "
        "WHERE d.AC_ACTION = 'DELETE' AND d.AC_ISUPDATE) b "
        f"JOIN (SELECT {', '.join(after_fields)} FROM {diff_sql} d "
        "WHERE d.AC_ACTION = 'INSERT' AND d.AC_ISUPDATE) a ON b.B_ROW_ID = a.A_ROW_ID"
    )


def page_sql(diff_sql: str, kind: ChangeKind, columns: Sequence[str]) -> str:
    """Select one page of a change set (bind ``LIMIT ?`` and ``OFFSET ?``).

    Update pairs use positional aliases because source identifiers may already
    occupy all 255 characters. The repository restores the display labels.
    """
    if kind is ChangeKind.UPDATED:
        pairs = ", ".join(
            f"b.{before}, a.{after}" for _, before, after in update_column_aliases(columns)
        )
        return (
            f"SELECT {pairs} {_update_join(diff_sql, columns)} ORDER BY b.B_ROW_ID LIMIT ? OFFSET ?"
        )
    action = "INSERT" if kind is ChangeKind.INSERTED else "DELETE"
    cols = ", ".join(quote_identifier(c) for c in columns)
    return (
        f"SELECT {cols} FROM {diff_sql} WHERE AC_ACTION = '{action}' AND NOT AC_ISUPDATE "
        "ORDER BY AC_ROW_ID LIMIT ? OFFSET ?"
    )


def column_changes_sql(diff_sql: str, columns: Sequence[str]) -> str:
    """Per-column number of updated rows where that column's value changed.

    Result columns are positional (``C0``, ``C1``...) to stay independent of the
    source's column naming; map them back with ``columns``.
    """
    sums = ", ".join(
        f"SUM(IFF(EQUAL_NULL(b.{before}, a.{after}), 0, 1)) AS C{i}"
        for i, (_, before, after) in enumerate(update_column_aliases(columns))
    )
    return f"SELECT {sums} {_update_join(diff_sql, columns)}"


def map_column_changes(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, int]:
    """Convert a :func:`column_changes_sql` row into ``{column: changed_rows}``."""
    return {c: int(row.get(f"C{i}") or 0) for i, c in enumerate(columns)}


def changed_columns(
    before: Mapping[str, Any], after: Mapping[str, Any], columns: Sequence[str]
) -> list[str]:
    """Columns whose value differs between two versions of a row (NULL-safe)."""
    return [c for c in columns if before.get(c) != after.get(c)]
