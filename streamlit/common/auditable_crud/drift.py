"""Detecting what happened to the source table since a change was dry-run.

The dry run captured ``snapshot`` (epoch nanoseconds) and cloned the source
*at* that instant. ``CHANGES(...) AT(TIMESTAMP => snapshot)`` on the source then
returns exactly the net changes committed by anyone else since then. We report
them and, using the table's key columns, how many of those rows are also rows
the change request touches ("overlap").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from auditable_crud.naming import quote_identifier


class DriftClass(StrEnum):
    """How the source changed relative to the change request."""

    NONE = "NO_DRIFT"
    DISJOINT = "DRIFT_NO_OVERLAP"
    OVERLAP = "DRIFT_OVERLAPS"


@dataclass(frozen=True)
class DriftReport:
    """Changes over one bounded interval, relative to one published change set."""

    baseline_ns: int
    end_ns: int
    diff_fqn: str
    inserted: int
    updated: int
    deleted: int
    overlapping_keys: int
    read_table_changes: int = 0

    @property
    def total(self) -> int:
        """Rows changed by others since the baseline, in the target and the tables it reads."""
        return self.inserted + self.updated + self.deleted + self.read_table_changes

    @property
    def classification(self) -> DriftClass:
        """Overall verdict used for gating approval and execution."""
        if self.total == 0:
            return DriftClass.NONE
        return DriftClass.OVERLAP if self.overlapping_keys else DriftClass.DISJOINT

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "baseline_ns": self.baseline_ns,
            "end_ns": self.end_ns,
            "diff_fqn": self.diff_fqn,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "total": self.total,
            "overlapping_keys": self.overlapping_keys,
            "read_table_changes": self.read_table_changes,
            "classification": str(self.classification),
        }


def _changes_clause(source_sql: str, baseline_ns: int, end_ns: int | None = None) -> str:
    # The baseline is an integer read back from CHANGE_REQUESTS, never user text;
    # the explicit check keeps this f-string injection-proof.
    if not isinstance(baseline_ns, int) or isinstance(baseline_ns, bool) or baseline_ns <= 0:
        raise ValueError("baseline_ns must be a positive integer")
    clause = (
        f"{source_sql} CHANGES(INFORMATION => DEFAULT) "
        f"AT(TIMESTAMP => TO_TIMESTAMP_LTZ({baseline_ns}, 9))"
    )
    if end_ns is not None:
        if type(end_ns) is not int or end_ns < baseline_ns:
            raise ValueError("end_ns must be an integer at or after baseline_ns")
        clause += f" END(TIMESTAMP => TO_TIMESTAMP_LTZ({end_ns}, 9))"
    return clause


def drift_counts_sql(source_sql: str, baseline_ns: int, end_ns: int | None = None) -> str:
    """Count rows inserted / updated / deleted in the source since the baseline."""
    return (
        "SELECT COUNT_IF(METADATA$ACTION = 'INSERT' AND NOT METADATA$ISUPDATE) AS INSERTED, "
        "COUNT_IF(METADATA$ACTION = 'INSERT' AND METADATA$ISUPDATE) AS UPDATED, "
        "COUNT_IF(METADATA$ACTION = 'DELETE' AND NOT METADATA$ISUPDATE) AS DELETED "
        f"FROM {_changes_clause(source_sql, baseline_ns, end_ns)}"
    )


def _key_projection(keys: Sequence[str], prefix: str) -> str:
    if not keys:
        raise ValueError("At least one key column is required for overlap detection")
    return ", ".join(f"s.{quote_identifier(k)} AS {prefix}{i}" for i, k in enumerate(keys))


def drift_overlap_sql(
    source_sql: str,
    baseline_ns: int,
    diff_sql: str,
    key_columns: Sequence[str],
    end_ns: int | None = None,
) -> str:
    """Count distinct keys changed both by others (drift) and by this request (diff)."""
    drifted_keys = _key_projection(key_columns, "D")
    touched_keys = _key_projection(key_columns, "T")
    matches = " AND ".join(f"EQUAL_NULL(d.D{i}, t.T{i})" for i in range(len(key_columns)))
    return (
        f"WITH drifted AS (SELECT DISTINCT {drifted_keys} "
        f"FROM {_changes_clause(source_sql, baseline_ns, end_ns)} s), "
        f"touched AS (SELECT DISTINCT {touched_keys} FROM {diff_sql} s) "
        f"SELECT COUNT(*) AS OVERLAPPING_KEYS FROM drifted d JOIN touched t ON {matches}"
    )


def overlap_column_name(source_columns: Sequence[str]) -> str:
    """Choose a drift flag name distinct from every business column."""
    occupied = set(source_columns)
    name = "AC_OVERLAPS"
    suffix = 0
    while name in occupied:
        suffix += 1
        name = f"AC_OVERLAPS_{suffix}"
    return name


def drift_rows_sql(
    source_sql: str,
    baseline_ns: int,
    diff_sql: str,
    key_columns: Sequence[str],
    source_columns: Sequence[str] | None = None,
    *,
    end_ns: int | None = None,
) -> str:
    """Drifted rows with a flag saying whether this request also touches them.

    Bind ``LIMIT ?``.
    """
    touched_keys = _key_projection(key_columns, "T")
    matches = " AND ".join(
        f"EQUAL_NULL(c.{quote_identifier(k)}, t.T{i})" for i, k in enumerate(key_columns)
    )
    overlap = quote_identifier(overlap_column_name(source_columns or ()))
    fields = (
        ", ".join(f"c.{quote_identifier(c)}" for c in source_columns)
        if source_columns
        else "c.* EXCLUDE (METADATA$ACTION, METADATA$ISUPDATE, METADATA$ROW_ID)"
    )
    return (
        f"WITH touched AS (SELECT DISTINCT {touched_keys} FROM {diff_sql} s) "
        "SELECT c.METADATA$ACTION AS AC_ACTION, c.METADATA$ISUPDATE AS AC_ISUPDATE, "
        f"EXISTS (SELECT 1 FROM touched t WHERE {matches}) AS {overlap}, {fields} "
        f"FROM {_changes_clause(source_sql, baseline_ns, end_ns)} c ORDER BY {overlap} DESC LIMIT ?"
    )


def build_report(
    baseline_ns: int,
    counts: Mapping[str, Any],
    overlap: Mapping[str, Any],
    read_table_changes: int = 0,
    *,
    end_ns: int,
    diff_fqn: str,
) -> DriftReport:
    """Combine the drift query results."""
    return DriftReport(
        baseline_ns=baseline_ns,
        end_ns=end_ns,
        diff_fqn=diff_fqn,
        inserted=int(counts.get("INSERTED") or 0),
        updated=int(counts.get("UPDATED") or 0),
        deleted=int(counts.get("DELETED") or 0),
        overlapping_keys=int(overlap.get("OVERLAPPING_KEYS") or 0),
        read_table_changes=read_table_changes,
    )
