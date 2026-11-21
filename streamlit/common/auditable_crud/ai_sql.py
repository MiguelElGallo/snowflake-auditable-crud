"""Natural-language-to-DML generation with Cortex ``AI_COMPLETE``.

Generated SQL is never executed automatically: it is placed in the editor and
goes through exactly the same guard, dry run and approval as hand-written SQL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from auditable_crud.sql_guard import TableRef

MAX_SAMPLE_ROWS = 5
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_DML_START = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|WITH)\b", re.IGNORECASE | re.MULTILINE)


def _format_value(value: Any) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def build_prompt(
    target: TableRef,
    columns: Sequence[tuple[str, str]],
    sample_rows: Sequence[Mapping[str, Any]],
    request: str,
    key_columns: Sequence[str] = (),
) -> str:
    """Compose the instruction sent to the model.

    The schema and a handful of sample rows ground the model; the instructions
    pin the output to one Snowflake DML statement against the target table.
    """
    schema = "\n".join(f"  {name} {dtype}" for name, dtype in columns)
    names = [name for name, _ in columns]
    sample = "\n".join(
        " | ".join(_format_value(row.get(n)) for n in names)
        for row in sample_rows[:MAX_SAMPLE_ROWS]
    )
    keys = ", ".join(key_columns) or "(none declared)"
    return (
        "You write a single Snowflake SQL DML statement.\n"
        f"Target table: {target}\n"
        f"Columns:\n{schema}\n"
        f"Primary/business key columns: {keys}\n"
        f"Sample rows ({' | '.join(names)}):\n{sample or '  (table is empty)'}\n\n"
        "Rules:\n"
        f"- Modify only {target}; use exactly one INSERT, UPDATE, DELETE or MERGE.\n"
        "- Do not use DDL, CALL, EXECUTE IMMEDIATE, user-defined functions or comments.\n"
        "- Prefer precise WHERE clauses; never update or delete every row unless asked.\n"
        "- Answer with the SQL only, inside one ```sql code block.\n\n"
        f"Change requested by the user:\n{request.strip()}\n"
    )


def extract_sql(response: str) -> str:
    """Pull the SQL statement out of a model response.

    Accepts a fenced code block or bare SQL with surrounding chatter. Returns
    an empty string if no DML-looking text is found; the guard later rejects
    anything that is not exactly one allowed statement.
    """
    text = response.strip()
    if text.startswith('"') and text.endswith('"'):
        # Some Snowflake result types serialize the answer as a JSON string.
        try:
            text = json.loads(text)
        except json.JSONDecodeError:
            return ""
    blocks = list(_FENCE.finditer(text))
    if blocks:
        if len(blocks) != 1:
            return ""
        block = blocks[0]
        if _DML_START.search(text[: block.start()]) or _DML_START.search(text[block.end() :]):
            return ""
        return block.group(1).strip()
    match = _DML_START.search(text)
    return text[match.start() :].strip() if match else ""
