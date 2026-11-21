"""Thin SQL execution boundary shared by the procedure and the apps.

Business logic depends only on :class:`SqlRunner`, so it can be unit-tested
with an in-memory fake and run unchanged on a Snowpark session.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any, Protocol

Row = dict[str, Any]

# Change this version whenever a statement can acquire different business semantics.
DML_CONTEXT_VERSION = 1
_DML_STATEMENT_PARAMS = {
    "BINARY_INPUT_FORMAT": "HEX",
    "BINARY_OUTPUT_FORMAT": "HEX",
    "DATE_INPUT_FORMAT": "YYYY-MM-DD",
    "DATE_OUTPUT_FORMAT": "YYYY-MM-DD",
    "ERROR_ON_NONDETERMINISTIC_MERGE": "TRUE",
    "ERROR_ON_NONDETERMINISTIC_UPDATE": "TRUE",
    "JSON_INDENT": "2",
    "STRICT_JSON_OUTPUT": "FALSE",
    "TIMEZONE": "UTC",
    "TIME_INPUT_FORMAT": "AUTO",
    "TIME_OUTPUT_FORMAT": "HH24:MI:SS",
    "TIMESTAMP_DAY_IS_ALWAYS_24H": "FALSE",
    "TIMESTAMP_INPUT_FORMAT": "AUTO",
    "TIMESTAMP_LTZ_OUTPUT_FORMAT": "YYYY-MM-DD HH24:MI:SS.FF3 TZHTZM",
    "TIMESTAMP_NTZ_OUTPUT_FORMAT": "YYYY-MM-DD HH24:MI:SS.FF3",
    "TIMESTAMP_OUTPUT_FORMAT": "YYYY-MM-DD HH24:MI:SS.FF3 TZHTZM",
    "TIMESTAMP_TYPE_MAPPING": "TIMESTAMP_NTZ",
    "TIMESTAMP_TZ_OUTPUT_FORMAT": "YYYY-MM-DD HH24:MI:SS.FF3 TZHTZM",
    "TWO_DIGIT_CENTURY_START": "1970",
    "WEEK_OF_YEAR_POLICY": "0",
    "WEEK_START": "0",
}


class SqlRunner(Protocol):
    """Executes one SQL statement with qmark (``?``) bind parameters."""

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        """Run ``sql`` and return every result row as a column-name -> value dict."""
        ...


class DmlRunner(SqlRunner, Protocol):
    """Executes approved DML with the same SQL semantics for every actor."""

    def query_dml(self, sql: str) -> list[Row]:
        """Run a guarded statement using the workflow's fixed statement parameters."""
        ...


class SnowparkRunner:
    """:class:`SqlRunner` backed by a ``snowflake.snowpark.Session``."""

    def __init__(self, session: Any) -> None:
        """Wrap an existing Snowpark session (typed ``Any``: Snowpark ships no stubs)."""
        self._session = session

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        """Execute ``sql`` through Snowpark with server-side parameter binding."""
        # Snowpark cannot bind Enum members (even str subclasses); send their values.
        bound = [p.value if isinstance(p, Enum) else p for p in params]
        rows = self._session.sql(sql, params=bound or None).collect()
        return [dict(row.as_dict()) for row in rows]

    def query_dml(self, sql: str) -> list[Row]:
        """Pin the caller-sensitive conversions and join policies for this statement."""
        # Snowpark adds its own plan metadata to the dictionary it receives.
        rows = self._session.sql(sql).collect(statement_params=_DML_STATEMENT_PARAMS.copy())
        return [dict(row.as_dict()) for row in rows]


def scalar(runner: SqlRunner, sql: str, params: Sequence[Any] = ()) -> Any:
    """Return the first column of the first row (``None`` when there are no rows)."""
    rows = runner.query(sql, params)
    return next(iter(rows[0].values())) if rows else None
