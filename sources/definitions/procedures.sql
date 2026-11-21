-- GENERATED FILE - do not edit. Source: streamlit/common/auditable_crud/*.py
-- Regenerate with: uv run python scripts/build_procedure.py

-- Owner's-rights entry points for every state-changing operation, one per app:
-- the developer app calls SP_DEV_API, the admin app and the cleanup task call
-- SP_ADMIN_API. Both run the same bundled code; each accepts only its own
-- operations (engine.DEVELOPER_OPS / engine.ADMIN_OPS). See engine.dispatch.

DEFINE PROCEDURE {{ app_db }}.CORE.SP_DEV_API(OP VARCHAR, ARGS VARIANT)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'sqlglot==30.17.0')
HANDLER = 'handler'
COMMENT = 'Auditable CRUD developer API (owner''s rights): cancel, create_request, drift, dry_run, submit'
EXECUTE AS OWNER
AS
$$
from __future__ import annotations

# ==================================================================
# auditable_crud/naming.py
# ==================================================================
"""Identifier handling and sandbox object naming.

Every identifier that ends up in dynamically built SQL goes through this module:
names are validated or sanitized, then always emitted double-quoted, so no user
supplied text can break out of an identifier position.
"""


import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_IDENTIFIER_LENGTH = 255
_UNQUOTED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_NON_IDENT_CHARS = re.compile(r"[^A-Z0-9_]+")
# Longest suffix appended to a sandbox base name (see SandboxNames).
_LONGEST_SUFFIX = len("_RV_STRM")
_SANDBOX_OBJECTS = frozenset(
    {"clone", "stream", "diff", "reval_clone", "reval_stream", "reval_diff"}
)


class IdentifierError(ValueError):
    """Raised when a configured identifier is not a safe Snowflake identifier."""


def normalize_identifier(value: str) -> str:
    """Return the canonical (upper-case) form of an unquoted Snowflake identifier.

    Configuration values are restricted to plain unquoted identifiers, which
    Snowflake resolves case-insensitively as upper case.
    """
    stripped = value.strip()
    if not _UNQUOTED_IDENTIFIER.match(stripped) or len(stripped) > MAX_IDENTIFIER_LENGTH:
        raise IdentifierError(f"Not a valid unquoted Snowflake identifier: {value!r}")
    return stripped.upper()


def quote_identifier(name: str) -> str:
    """Quote an identifier exactly as given, escaping embedded double quotes."""
    if not name:
        raise IdentifierError("Identifier must not be empty")
    return '"' + name.replace('"', '""') + '"'


def fqn(database: str, schema: str, name: str) -> str:
    """Build a fully qualified, fully quoted three-part name."""
    return ".".join(quote_identifier(part) for part in (database, schema, name))


def information_schema(database: str) -> str:
    """Quoted ``"DB".INFORMATION_SCHEMA`` prefix for metadata queries."""
    return f"{quote_identifier(database)}.INFORMATION_SCHEMA"


def sql_string_literal(value: str) -> str:
    """Render ``value`` as a Snowflake single-quoted string literal.

    Only for DDL positions that do not accept bind variables (e.g. ``COMMENT``);
    everywhere else values are bound. Backslash is Snowflake's escape character
    inside single quotes, so it is doubled along with the quote itself.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sanitize_name_part(value: str, max_length: int) -> str:
    """Turn arbitrary text (e.g. a user name) into an identifier fragment."""
    cleaned = _NON_IDENT_CHARS.sub("_", value.upper()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned) or "X"
    return cleaned[:max_length].rstrip("_") or "X"


@dataclass(frozen=True)
class SandboxNames:
    """Unquoted object names for one change request's sandbox objects.

    ``base`` identifies the request: source table, timestamp, user and the
    request id, so a clone can be traced back to its owner just from its name.
    """

    base: str

    def for_revision(self, revision: int) -> SandboxNames:
        """Return distinct object names for one immutable run of this request."""
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision < 10**38
        ):
            raise ValueError("Sandbox revision must be a non-negative Snowflake integer.")
        if revision == 0:
            return self
        suffix = f"_R{revision}"
        room = MAX_IDENTIFIER_LENGTH - _LONGEST_SUFFIX - len(suffix)
        base = self.base
        if len(base) > room:
            # Retain the beginning of the table name and the timestamp, actor and
            # request ID at the end. The hash distinguishes legacy names which
            # differ only in the portion of the table name that was shortened.
            prefix = base[:16] + "_" + hashlib.sha256(base.encode()).hexdigest()[:16].upper() + "_"
            base = prefix + base[-(room - len(prefix)) :]
        return SandboxNames(base + suffix)

    def published(self, request: Mapping[str, Any], kind: str) -> str:
        """Resolve a request's published artifact, including pre-revision object names."""
        if kind not in _SANDBOX_OBJECTS:
            raise ValueError(f"Unknown sandbox object kind {kind!r}")
        field = "REVAL_SANDBOX_REVISION" if kind.startswith("reval_") else "DRY_SANDBOX_REVISION"
        revision = int(request.get(field) or 0)
        attribute = kind.removeprefix("reval_") if revision else kind
        return str(getattr(self.for_revision(revision), attribute))

    @property
    def clone(self) -> str:
        """Clone of the source table that the developer's DML runs against."""
        return self.base

    @property
    def stream(self) -> str:
        """Stream on the clone capturing exactly the rows the DML touched."""
        return f"{self.base}_STRM"

    @property
    def diff(self) -> str:
        """Materialized copy of the stream contents (stable, browsable)."""
        return f"{self.base}_DIFF"

    @property
    def reval_clone(self) -> str:
        """Clone used by the admin's re-validation against current data."""
        return f"{self.base}_RV"

    @property
    def reval_stream(self) -> str:
        """Stream on the re-validation clone."""
        return f"{self.base}_RV_STRM"

    @property
    def reval_diff(self) -> str:
        """Materialized diff of the re-validation run."""
        return f"{self.base}_RV_DIFF"


def build_sandbox_names(
    source_table: str, user_name: str, request_id: str, created_at: datetime
) -> SandboxNames:
    """Derive unique, identifiable sandbox names for a change request.

    Format: ``<SOURCE>__CR_<yyyymmddHHMMSS>_<USER>_<request UUID hex>``. The source part is
    truncated first so every derived name stays within Snowflake's 255-char limit.
    """
    stamp = created_at.strftime("%Y%m%d%H%M%S")
    user = sanitize_name_part(user_name, 40)
    request_token = sanitize_name_part(request_id.replace("-", ""), 32)
    tail = f"__CR_{stamp}_{user}_{request_token}"
    room = MAX_IDENTIFIER_LENGTH - _LONGEST_SUFFIX - len(tail)
    source = sanitize_name_part(source_table, room)
    return SandboxNames(base=f"{source}{tail}")

# ==================================================================
# auditable_crud/sql_guard.py
# ==================================================================
"""Static validation and target rewriting of user-supplied DML.

The guard is the security boundary between free-form SQL typed (or generated)
in the app and statements executed by an owner's-rights procedure. It runs in
the app for fast feedback and again inside the procedure before every
execution; only the procedure-side check is authoritative.

Rules enforced:

* exactly one statement, of type INSERT, UPDATE, DELETE or MERGE;
* the written table is the configured managed table;
* every other table read is either the managed table or explicitly allowed;
* reads use the current version of each physical table;
* sampling, row limits and operations that choose arbitrary rows are rejected;
  clock values are restricted to scalar assignments and explicit inserted values;
* no DDL, ``CALL``, ``EXECUTE IMMEDIATE``, session commands, table functions,
  ``INSERT OVERWRITE``/multi-table inserts, or functions outside the reviewed
  Snowflake built-ins.

Rewriting is done on the AST (never by string replacement) and every table
reference is emitted fully qualified, so the statement means the same thing
regardless of the session's current database and schema.
"""


import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError


MAX_SQL_LENGTH = 100_000
DIALECT = "snowflake"

# SQLGlot's typed function registry spans many SQL dialects. Check the emitted
# operation even when the parser recognized the name. These are SQLGlot node
# types for the Snowflake expressions used for filtering and transforming data.
_ALLOWED_FUNCTION_NODES: frozenset[type[exp.Func]] = frozenset(
    {
        # Conditions and type conversion
        exp.And,
        exp.Or,
        exp.Case,
        exp.Cast,
        exp.Coalesce,
        exp.DecodeCase,
        exp.EqualNull,
        exp.Exists,
        exp.Greatest,
        exp.If,
        exp.Least,
        exp.Nullif,
        exp.Nvl2,
        exp.TryCast,
        # Numeric
        exp.Abs,
        exp.Acos,
        exp.Asin,
        exp.Atan,
        exp.Atan2,
        exp.Ceil,
        exp.Cos,
        exp.Degrees,
        exp.Exp,
        exp.Floor,
        exp.Ln,
        exp.Log,
        exp.Pi,
        exp.Pow,
        exp.Radians,
        exp.Round,
        exp.Sign,
        exp.Sin,
        exp.Sqrt,
        exp.Tan,
        exp.Trunc,
        exp.WidthBucket,
        # Date and time
        exp.AddMonths,
        exp.ConvertTimezone,
        exp.CurrentDate,
        exp.CurrentTime,
        exp.CurrentTimestamp,
        exp.DateAdd,
        exp.DateDiff,
        exp.DateFromParts,
        exp.DateTrunc,
        exp.Day,
        exp.DayOfWeek,
        exp.DayOfWeekIso,
        exp.DayOfYear,
        exp.Extract,
        exp.Hour,
        exp.LastDay,
        exp.Localtime,
        exp.Localtimestamp,
        exp.Minute,
        exp.Month,
        exp.Quarter,
        exp.Second,
        exp.StrToTime,
        exp.TimeFromParts,
        exp.TimeSlice,
        exp.TimestampFromParts,
        exp.TimestampTrunc,
        exp.TsOrDsToDate,
        exp.TsOrDsToTime,
        exp.UnixToTime,
        exp.Week,
        exp.WeekOfYear,  # Snowflake WEEKISO
        exp.Year,
        exp.YearOfWeek,
        exp.YearOfWeekIso,
        # Text
        exp.Ascii,
        exp.ByteLength,
        exp.Chr,
        exp.Concat,
        exp.ConcatWs,
        exp.Contains,
        exp.EndsWith,
        exp.HexDecodeString,
        exp.Initcap,
        exp.Left,
        exp.Length,
        exp.Lower,
        exp.Pad,
        exp.RegexpCount,
        exp.RegexpExtract,
        exp.RegexpInstr,
        exp.RegexpLike,
        exp.RegexpReplace,
        exp.Repeat,
        exp.Replace,
        exp.Reverse,
        exp.Right,
        exp.Split,
        exp.SplitPart,
        exp.StartsWith,
        exp.StrPosition,
        exp.Substring,
        exp.ToBinary,
        exp.ToBoolean,
        exp.ToChar,
        exp.ToDouble,
        exp.ToNumber,
        exp.Translate,
        exp.Trim,
        exp.Upper,
        # Aggregates and windows
        exp.Avg,
        exp.Count,
        exp.CountIf,
        exp.CumeDist,
        exp.DenseRank,
        exp.LogicalAnd,
        exp.LogicalOr,
        exp.Max,
        exp.Median,
        exp.Min,
        exp.PercentRank,
        exp.Rank,
        exp.Stddev,
        exp.StddevPop,
        exp.StddevSamp,
        exp.Sum,
        exp.Variance,
        exp.VariancePop,
        # Semi-structured values, hashing and identifiers
        exp.Array,
        exp.ArrayAppend,
        exp.ArrayCompact,
        exp.ArrayConcat,
        exp.ArrayContains,
        exp.ArrayDistinct,
        exp.ArraySize,
        exp.GetExtract,
        exp.IsNullValue,
        exp.JSONExtract,
        exp.JSONFormat,
        exp.JSONObject,
        exp.MD5,
        exp.ObjectAgg,
        exp.ObjectInsert,
        exp.ParseJSON,
        exp.SHA,
        exp.SHA2,
        exp.Struct,
        exp.ToVariant,
        exp.Typeof,
        exp.Uuid,
    }
)
# Some native functions are represented by their SQL names rather than a typed
# operation. Keep quoted or schema-qualified routine names outside this set.
ALLOWED_UNTYPED_FUNCTIONS = frozenset(
    {
        "HASH",
        "NULLIFZERO",
        "OBJECT_DELETE",
        "TO_TIMESTAMP",
        "TO_TIMESTAMP_LTZ",
        "TO_TIMESTAMP_NTZ",
        "TO_TIMESTAMP_TZ",
        "TRY_TO_TIMESTAMP",
        "TRY_TO_TIMESTAMP_LTZ",
        "TRY_TO_TIMESTAMP_NTZ",
        "TRY_TO_TIMESTAMP_TZ",
        "ZEROIFNULL",
    }
)

_FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = (
    exp.Command,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Use,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Lateral,
    exp.TableFromRows,
    exp.MultitableInserts,
    exp.Parameter,
    exp.SessionParameter,
    exp.Placeholder,
)
_DML_TYPES: dict[type[exp.Expr], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Merge: "MERGE",
}

# A clone and its source can scan equal sort keys in different physical orders.
# Without provably unique ordering, limits, positional windows, tied extrema and
# order-sensitive aggregates can choose different business keys on identical data.
_NONREPEATABLE_OPERATIONS = (
    exp.TableSample,
    exp.Rand,
    exp.Limit,
    exp.Offset,
    exp.Fetch,
    exp.MatchRecognize,
    exp.AnyValue,
    exp.ArgMin,
    exp.ArgMax,
    exp.ArrayAgg,
    exp.GroupConcat,
    exp.FirstValue,
    exp.LastValue,
    exp.NthValue,
    exp.Lag,
    exp.Lead,
    exp.Ntile,
    exp.RowNumber,
)
_CLOCK_FUNCTIONS = (
    exp.CurrentDate,
    exp.CurrentTime,
    exp.CurrentTimestamp,
    exp.Localtime,
    exp.Localtimestamp,
)


class GuardError(ValueError):
    """Raised when a statement is not an acceptable change-request DML."""


@dataclass(frozen=True, order=True)
class TableRef:
    """A resolved three-part table name (identifier text exactly as Snowflake stores it)."""

    database: str
    schema: str
    name: str

    @classmethod
    def from_config(cls, database: str, schema: str, name: str) -> TableRef:
        """Build a reference from unquoted configuration identifiers."""
        return cls(
            normalize_identifier(database),
            normalize_identifier(schema),
            normalize_identifier(name),
        )

    @classmethod
    def parse(cls, dotted: str) -> TableRef:
        """Parse an unquoted ``DB.SCHEMA.TABLE`` configuration string."""
        parts = dotted.split(".")
        if len(parts) != 3:
            raise GuardError(f"Expected DB.SCHEMA.TABLE, got {dotted!r}")
        return cls.from_config(*parts)

    @property
    def sql(self) -> str:
        """Fully quoted SQL form."""
        return fqn(self.database, self.schema, self.name)

    def __str__(self) -> str:
        """Unquoted dotted form for messages."""
        return f"{self.database}.{self.schema}.{self.name}"


@dataclass(frozen=True)
class GuardedStatement:
    """A validated DML statement that can be rendered against any table."""

    kind: str
    target: TableRef
    reads: frozenset[TableRef]
    canonical_sql: str
    _expression: exp.Expr = field(repr=False, compare=False)

    def render_for(self, replacement: TableRef, *, lookup_snapshot_ns: int | None = None) -> str:
        """Replace the target and optionally read each physical lookup at the clone's instant."""
        if lookup_snapshot_ns is not None and (
            type(lookup_snapshot_ns) is not int or lookup_snapshot_ns <= 0
        ):
            raise ValueError("lookup_snapshot_ns must be a positive integer")
        tree = self._expression.copy()
        for table in tree.find_all(exp.Table):
            # Only physical tables were qualified by the guard. An unqualified
            # relation here is a CTE and can have the same name as the target.
            if not table.args.get("catalog"):
                continue
            ref = _resolve(table, self.target)
            if ref == self.target:
                _set_table(table, replacement)
            elif lookup_snapshot_ns is not None and ref in self.reads:
                table.set(
                    "when",
                    exp.HistoricalData(
                        this="AT",
                        kind="TIMESTAMP",
                        expression=exp.Anonymous(
                            this="TO_TIMESTAMP_LTZ",
                            expressions=[
                                exp.Literal.number(lookup_snapshot_ns),
                                exp.Literal.number(9),
                            ],
                        ),
                    ),
                )
        # Canonical SQL fully qualifies columns that name the target table.
        for column in tree.find_all(exp.Column):
            if _column_qualifier(column) == self.target:
                _set_column_qualifier(column, replacement)
        return _render_sql(tree)


def _ident_text(node: exp.Expr | None) -> str:
    if not isinstance(node, exp.Identifier):
        return ""
    return str(node.this) if node.quoted else str(node.this).upper()


def _resolve(table: exp.Table, default: TableRef) -> TableRef:
    if table.args.get("catalog") and not _ident_text(table.args.get("db")):
        raise GuardError("Specify the schema explicitly in database-qualified table names.")
    return TableRef(
        _ident_text(table.args.get("catalog")) or default.database,
        _ident_text(table.args.get("db")) or default.schema,
        _ident_text(table.this),
    )


def _set_table(table: exp.Table, ref: TableRef) -> None:
    table.set("catalog", exp.to_identifier(ref.database, quoted=True))
    table.set("db", exp.to_identifier(ref.schema, quoted=True))
    table.set("this", exp.to_identifier(ref.name, quoted=True))


def _column_qualifier(column: exp.Column) -> TableRef | None:
    """Return the fully qualified table a column names, if it is fully qualified."""
    parts = [_ident_text(column.args.get(key)) for key in ("catalog", "db", "table")]
    return TableRef(*parts) if all(parts) else None


def _set_column_qualifier(column: exp.Column, ref: TableRef) -> None:
    column.set("catalog", exp.to_identifier(ref.database, quoted=True))
    column.set("db", exp.to_identifier(ref.schema, quoted=True))
    column.set("table", exp.to_identifier(ref.name, quoted=True))


def _source_bindings(
    tree: exp.Expr, target: TableRef
) -> dict[tuple[int, tuple[str, ...]], list[TableRef | None]]:
    """Map names visible in each query block to physical tables or local aliases."""
    bindings: dict[tuple[int, tuple[str, ...]], list[TableRef | None]] = {}
    for node in tree.walk():
        alias = node.args.get("alias")
        if isinstance(alias, exp.TableAlias) and not isinstance(node, exp.CTE):
            name, ref = _ident_text(alias.this), None
        elif isinstance(node, exp.Table):
            name = _ident_text(node.this)
            ref = _resolve(node, target) if node.args.get("catalog") else None
        else:
            continue
        scope = node.find_ancestor(exp.Select, exp.DML, exp.SetOperation)
        if scope is not None:
            qualifiers = (
                [(name,)]
                if ref is None
                else [(name,), (ref.schema, name), (ref.database, ref.schema, name)]
            )
            for qualifier in qualifiers:
                bindings.setdefault((id(scope), qualifier), []).append(ref)
    return bindings


def _qualify_columns(tree: exp.Expr, target: TableRef) -> None:
    """Qualify physical table columns, preserving local aliases and CTE references."""
    bindings = _source_bindings(tree, target)
    for column in tree.find_all(exp.Column):
        if not column.table:
            continue
        qualifier = tuple(
            _ident_text(column.args[key])
            for key in ("catalog", "db", "table")
            if column.args.get(key)
        )
        scope = column.parent
        while scope is not None:
            matches = bindings.get((id(scope), qualifier))
            if matches:
                if len(matches) > 1:
                    raise GuardError(
                        f"Ambiguous column qualifier {'.'.join(qualifier)}. Use table aliases."
                    )
                if matches[0] is not None:
                    _set_column_qualifier(column, matches[0])
                break
            scope = scope.parent


def _refers_to_cte(table: exp.Table) -> bool:
    """Whether an unqualified ``table`` names a CTE that is in scope where it appears.

    Walks up to each enclosing ``WITH``. Inside the ``WITH`` itself only CTEs
    defined earlier are visible (recursive CTEs are not supported), so a name that
    shadows a real table elsewhere in the statement cannot hide that table.
    """
    if table.args.get("db") or table.args.get("catalog"):
        return False
    name = _ident_text(table.this)
    child: exp.Expr = table
    node = table.parent
    while node is not None:
        if isinstance(node, exp.With):
            for cte in node.expressions:
                cte_name = _ident_text(cte.args["alias"].this)
                if cte is child:
                    if cte_name == name:
                        raise GuardError(
                            "Recursive CTEs are not supported. Qualify a physical table "
                            "that has the same name as its CTE."
                        )
                    break  # CTEs from here on are not yet defined
                if cte_name == name:
                    return True
        else:
            with_ = node.args.get("with_") if node.args else None
            if (
                isinstance(with_, exp.With)
                and child is not with_
                and name in {_ident_text(c.args["alias"].this) for c in with_.expressions}
            ):
                return True
        child, node = node, node.parent
    return False


def _parse_single(sql: str) -> exp.Expr:
    if not sql.strip():
        raise GuardError("The statement is empty.")
    if len(sql) > MAX_SQL_LENGTH:
        raise GuardError(f"The statement exceeds {MAX_SQL_LENGTH} characters.")
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (SqlglotError, RecursionError) as error:
        raise GuardError(f"Could not parse the statement: {error}") from error
    if len(statements) != 1:
        raise GuardError(f"Exactly one statement is allowed, found {len(statements)}.")
    return statements[0]


def _render_sql(tree: exp.Expr) -> str:
    try:
        return tree.sql(dialect=DIALECT, unsupported_level=ErrorLevel.RAISE)
    except (SqlglotError, RecursionError) as error:
        raise GuardError(f"Could not render the Snowflake statement: {error}") from error


def _is_sequence_reference(node: exp.Expr) -> bool:
    return isinstance(node, exp.NextValueFor) or (
        isinstance(node, exp.Column) and bool(node.table) and _ident_text(node.this) == "NEXTVAL"
    )


def has_sequence_default(expression: str | None) -> bool:
    """Detect defaults that could advance an external sequence on a table clone."""
    if not expression or not expression.strip():
        return False
    # Identity columns use Snowflake's own identity generator. Some metadata
    # surfaces describe them using their column DDL instead of a scalar default.
    if re.fullmatch(
        r"(?:AUTOINCREMENT|IDENTITY)(?:\s*\(\s*-?\d+\s*,\s*-?\d+\s*\)|"
        r"\s+START\s+-?\d+\s+INCREMENT\s+-?\d+)?(?:\s+(?:NOORDER|ORDER))?",
        expression.strip(),
        re.IGNORECASE,
    ):
        return False
    try:
        tree = _parse_single(expression)
    except GuardError:
        return True
    return isinstance(tree, (exp.Command, exp.DDL, exp.DML)) or any(
        _is_sequence_reference(node) for node in tree.walk()
    )


def _in_write_value(node: exp.Expr) -> bool:
    """Whether this expression supplies a scalar SET assignment or explicit INSERT value."""
    child = node
    while (parent := child.parent) is not None:
        # A projected clock can later affect WHERE, JOIN, HAVING, QUALIFY or a
        # correlated subquery through an alias. Do not infer dataflow across queries.
        if isinstance(parent, (exp.Query, exp.Window)):
            return False
        if isinstance(parent, exp.Insert):
            return child is parent.expression and isinstance(child, (exp.Values, exp.Tuple))
        if (
            isinstance(parent, exp.EQ)
            and child is parent.expression
            and isinstance(parent.parent, exp.Update)
            and parent.arg_key == "expressions"
        ):
            return True
        child = parent
    return False


def _check_repeatable_rows(node: exp.Expr) -> None:
    if (
        isinstance(node, _NONREPEATABLE_OPERATIONS)
        or (isinstance(node, exp.Distinct) and node.args.get("on"))
        or (isinstance(node, exp.WindowSpec) and node.text("kind").upper() == "ROWS")
        or (isinstance(node, exp.Join) and node.method == "ASOF")
        or (isinstance(node, exp.Uuid) and not (node.this and node.args.get("name")))
    ):
        raise GuardError(
            f"{type(node).__name__} cannot guarantee repeatable results for the approved change. "
            "Use an explicit key or a deterministic aggregate."
        )
    if isinstance(node, _CLOCK_FUNCTIONS) and not _in_write_value(node):
        raise GuardError(
            "Use a fixed date or time to select rows. Clock values require a write assignment."
        )


def _check_forbidden(tree: exp.Expr) -> None:
    for node in tree.walk():
        _check_repeatable_rows(node)
        if isinstance(node, _FORBIDDEN_NODES) or (
            isinstance(node, exp.UDTF) and not isinstance(node, exp.Values)
        ):
            raise GuardError(f"{type(node).__name__} is not allowed in a change request.")
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            name = node.expression.name or node.expression.sql_name()
            raise GuardError(f"Qualified function {name.upper()}() is not allowed.")
        if _is_sequence_reference(node):
            raise GuardError("Sequence NEXTVAL references are not allowed in a sandbox.")
        if isinstance(node, exp.Func) and not _allowed_function(node):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            raise GuardError(
                f"Function {name}() is not an allowed built-in "
                "(user-defined and system functions are blocked)."
            )
        # MERGE actions are represented by nested DML nodes in SQLGlot.
        if (
            isinstance(node, exp.DML)
            and node is not tree
            and not (
                isinstance(tree, exp.Merge)
                and isinstance(node.parent, exp.When)
                and node.arg_key == "then"
            )
        ):
            raise GuardError("Only the top-level change may write to a table.")


def _allowed_function(node: exp.Func) -> bool:
    if isinstance(node, exp.Anonymous):
        return node.name.upper() in ALLOWED_UNTYPED_FUNCTIONS and not (
            isinstance(node.this, exp.Identifier) and node.this.quoted
        )
    return type(node) in _ALLOWED_FUNCTION_NODES


def _preserve_epoch_types(tree: exp.Expr) -> None:
    """Keep explicit epoch overloads that SQLGlot's Snowflake generator emits as TO_TIMESTAMP."""
    names = {
        exp.DType.TIMESTAMPLTZ: "TO_TIMESTAMP_LTZ",
        exp.DType.TIMESTAMPNTZ: "TO_TIMESTAMP_NTZ",
        exp.DType.TIMESTAMPTZ: "TO_TIMESTAMP_TZ",
    }
    for conversion in list(tree.find_all(exp.UnixToTime)):
        target = conversion.args.get("target_type")
        if isinstance(target, exp.DataType) and target.this in names:
            args = [conversion.this]
            if scale := conversion.args.get("scale"):
                args.append(scale)
            conversion.replace(exp.Anonymous(this=names[target.this], expressions=args))


def _written_table(tree: exp.Expr) -> exp.Table:
    written = tree.this
    if isinstance(written, exp.Schema):  # INSERT INTO t (col, ...)
        written = written.this
    if not isinstance(written, exp.Table) or not isinstance(written.this, exp.Identifier):
        raise GuardError("Could not determine the table being modified.")
    return written


def guard_dml(
    sql: str, target: TableRef, allowed_reads: frozenset[TableRef] = frozenset()
) -> GuardedStatement:
    """Validate ``sql`` as a change to ``target`` and return its canonical form.

    Unqualified table names resolve against the target's database and schema.

    Raises:
        GuardError: if any rule in the module docstring is violated.

    """
    tree = _parse_single(sql)
    kind = _DML_TYPES.get(type(tree))
    if kind is None:
        raise GuardError("Only INSERT, UPDATE, DELETE and MERGE statements are allowed.")
    if tree.args.get("with_"):
        raise GuardError("Snowflake requires WITH inside the SELECT source of a DML statement.")
    if isinstance(tree, exp.Insert) and tree.args.get("overwrite"):
        raise GuardError("INSERT OVERWRITE is not allowed.")
    write_target = _written_table(tree)
    written = _resolve(write_target, target)
    if written != target:
        raise GuardError(f"The statement modifies {written}, but this request is for {target}.")

    reads: set[TableRef] = set()
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier):
            kind_name = type(table.this).__name__
            raise GuardError(
                f"Table functions ({kind_name}) and computed table references are not allowed."
            )
        if any(table.args.get(key) for key in ("when", "changes", "version")):
            raise GuardError("Time Travel and CHANGES are not allowed on change-request tables.")
        if table is not write_target and _refers_to_cte(table):
            continue
        ref = _resolve(table, target)
        if ref != target and ref not in allowed_reads:
            raise GuardError(
                f"Reading {ref} is not allowed for this table. "
                "Ask an admin to add it to allowed_read_tables."
            )
        _set_table(table, ref)
        if ref != target:
            reads.add(ref)
    _check_forbidden(tree)
    _qualify_columns(tree, target)
    _preserve_epoch_types(tree)

    return GuardedStatement(
        kind=kind,
        target=target,
        reads=frozenset(reads),
        canonical_sql=_render_sql(tree),
        _expression=tree,
    )

# ==================================================================
# auditable_crud/state.py
# ==================================================================
"""Change-request lifecycle and separation-of-duties rules.

``DRAFT -> DRY_RUN_OK -> SUBMITTED -> APPROVED -> EXECUTING -> EXECUTED | FAILED``

* A dry run may be repeated (``DRAFT|DRY_RUN_OK -> DRY_RUN_OK``).
* An admin may reject a submitted request, or send an approved one back to
  ``SUBMITTED`` when the source changed after approval.
* The requester may cancel any request that has not started executing.
"""


from enum import StrEnum


class Status(StrEnum):
    """States of a change request (stored verbatim in CHANGE_REQUESTS.STATUS)."""

    DRAFT = "DRAFT"
    DRY_RUN_OK = "DRY_RUN_OK"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DRAFT: frozenset({Status.DRY_RUN_OK, Status.CANCELLED}),
    Status.DRY_RUN_OK: frozenset({Status.DRY_RUN_OK, Status.SUBMITTED, Status.CANCELLED}),
    Status.SUBMITTED: frozenset({Status.APPROVED, Status.REJECTED, Status.CANCELLED}),
    # After a rolled-back execution the durable state is still APPROVED. Record
    # the failed attempt in its own transaction guarded by the approval revision.
    Status.APPROVED: frozenset(
        {Status.EXECUTING, Status.FAILED, Status.SUBMITTED, Status.CANCELLED}
    ),
    Status.EXECUTING: frozenset({Status.EXECUTED, Status.FAILED}),
    Status.EXECUTED: frozenset(),
    Status.FAILED: frozenset(),
    Status.REJECTED: frozenset(),
    Status.CANCELLED: frozenset(),
}

TERMINAL_STATUSES = frozenset(s for s, targets in TRANSITIONS.items() if not targets)
EDITABLE_STATUSES = frozenset({Status.DRAFT, Status.DRY_RUN_OK})


class WorkflowError(ValueError):
    """Raised when an action is not permitted in the request's current state."""


def ensure_transition(current: str, new: Status) -> Status:
    """Validate ``current -> new`` and return the parsed current status."""
    try:
        status = Status(current)
    except ValueError as error:
        raise WorkflowError(f"Unknown status {current!r}") from error
    if new not in TRANSITIONS[status]:
        raise WorkflowError(f"A request in status {status} cannot move to {new}.")
    return status


def same_user(a: str, b: str) -> bool:
    """Compare Snowflake user names (case-insensitive, like unquoted identifiers)."""
    return a.strip().upper() == b.strip().upper()


def ensure_not_requester(requested_by: str, actor: str, action: str) -> None:
    """Enforce separation of duties: requesters never review or run their own change."""
    if same_user(requested_by, actor):
        raise WorkflowError(f"{actor} requested this change and cannot {action} it.")


def ensure_requester(requested_by: str, actor: str, action: str) -> None:
    """Only the requester may edit, dry-run, submit or cancel their request."""
    if not same_user(requested_by, actor):
        raise WorkflowError(f"Only {requested_by} can {action} this request.")

# ==================================================================
# auditable_crud/diff.py
# ==================================================================
"""Turning a stream on the clone into a precise, browsable change set.

The stream is created on the clone *before* the DML runs, so afterwards it
holds exactly the rows the statement touched. Its contents are copied into a
"diff" table (``METADATA$`` columns renamed to ``AC_*``) so that the result is
stable and can be browsed by developer and admin long after the dry run.

An UPDATE shows up as a DELETE/INSERT pair sharing ``METADATA$ROW_ID`` with
``METADATA$ISUPDATE = TRUE``: the DELETE side is the row before, the INSERT
side the row after.
"""


from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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

# ==================================================================
# auditable_crud/drift.py
# ==================================================================
"""Detecting what happened to the source table since a change was dry-run.

The dry run captured ``snapshot`` (epoch nanoseconds) and cloned the source
*at* that instant. ``CHANGES(...) AT(TIMESTAMP => snapshot)`` on the source then
returns exactly the net changes committed by anyone else since then. We report
them and, using the table's key columns, how many of those rows are also rows
the change request touches ("overlap").
"""


from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any



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

# ==================================================================
# auditable_crud/config.py
# ==================================================================
"""Managed-table configuration, read from the DCM-defined ``CORE.V_MANAGED_TABLES`` view."""


from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


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

# ==================================================================
# auditable_crud/runner.py
# ==================================================================
"""Thin SQL execution boundary shared by the procedure and the apps.

Business logic depends only on :class:`SqlRunner`, so it can be unit-tested
with an in-memory fake and run unchanged on a Snowpark session.
"""


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

# ==================================================================
# auditable_crud/store.py
# ==================================================================
"""Persistence for change requests and the audit log (``CORE`` schema tables)."""


import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol


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

# ==================================================================
# auditable_crud/engine.py
# ==================================================================
"""Change-request workflow, shared by the two owner's-rights procedures.

Every state change re-validates its preconditions server-side: the apps are
convenience UIs, this module is the authority. Status changes use
compare-and-set (``UPDATE ... WHERE STATUS = <expected>``) so double clicks or
two admins acting at once cannot both win.
"""


import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


RESERVED_COLUMNS = frozenset({"AC_ACTION", "AC_ISUPDATE", "AC_ROW_ID"})
MAX_TEXT = 10_000


def sha256(text: str) -> str:
    """Hex SHA-256 of ``text`` (used to pin the approved statement)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def within_tolerance(expected: DiffCounts, actual: DiffCounts, tolerance_pct: float) -> bool:
    """Return True when inserted/updated/deleted are each within ``tolerance_pct``."""
    for exp_value, act_value in (
        (expected.inserted, actual.inserted),
        (expected.updated, actual.updated),
        (expected.deleted, actual.deleted),
    ):
        if 100 * abs(act_value - exp_value) > exp_value * tolerance_pct:
            return False
    return True


def _clean_text(value: Any, field: str, *, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise WorkflowError(f"{field} is required.")
    if len(text) > MAX_TEXT:
        raise WorkflowError(f"{field} is longer than {MAX_TEXT} characters.")
    return text


def _sandbox_label(request_id: str, actor: str, source: TableRef, purpose: str) -> str:
    return f"Auditable CRUD {purpose} for request {request_id} by {actor} on {source}"


def _number(args: Mapping[str, Any], key: str, default: float) -> float:
    """Numeric argument with a default; unlike ``x or default``, keeps an explicit 0."""
    value = args.get(key)
    return default if value is None or value == "" else float(value)


def _days(args: Mapping[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    if type(value) is int or (isinstance(value, str) and value.isdecimal()):
        days = int(value)
        if days >= 0:
            return days
    raise WorkflowError(f"{key} must be a non-negative whole number of days.")


def _counts(value: Any) -> DiffCounts:
    return DiffCounts.from_row({k.upper(): v for k, v in (value or {}).items()})


class _ExecutionCountError(WorkflowError):
    """Snowflake reported ambiguous update matches or counts outside the approval."""


class _RevalidationRequiredError(WorkflowError):
    """The recorded sandbox no longer provides an approval baseline."""

    def __init__(self, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(f"{reason} Re-validate on current data before approving.")
        self.reason = reason
        self.details = details


class _DefinitionChangedError(_RevalidationRequiredError):
    """A table definition can no longer be verified against the reviewed run."""


@dataclass
class _DmlAttempt:
    """Evidence obtained from the source statement, including a rolled-back attempt."""

    query_id: str | None = None
    elapsed_ms: int | None = None
    counts: DiffCounts | None = None

    def details(self) -> Row:
        values: Row = {}
        if self.query_id:
            values["query_id"] = self.query_id
        if self.elapsed_ms is not None:
            values["elapsed_ms"] = self.elapsed_ms
        if self.counts is not None:
            values["counts"] = self.counts.as_dict()
        return values


class Engine:
    """Implements every change-request operation."""

    def __init__(
        self,
        runner: DmlRunner,
        store: RequestStore,
        *,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """Create an engine; ``new_id`` and ``clock`` are injectable for tests."""
        self._sql = runner
        self._store = store
        self._new_id = new_id
        self._clock = clock
        self._rollback_uncertain = False

    def _rollback(self, error: Exception) -> None:
        try:
            self._store.rollback()
        except Exception as rollback_error:
            # More SQL on this session, especially DDL, could commit the failed operation.
            self._rollback_uncertain = True
            raise error from rollback_error

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        self._store.begin()
        try:
            yield
            self._store.commit()
        except Exception as error:
            self._rollback(error)
            raise

    # ------------------------------------------------------------------ helpers
    def _config(self, table_id: str) -> TableConfig:
        return TableConfig.from_row(self._store.load_config(table_id))

    def _request_config(self, request: Mapping[str, Any]) -> TableConfig:
        cfg = self._config(str(request["TABLE_ID"]))
        if str(cfg.source) != request["SOURCE_FQN"]:
            raise WorkflowError(
                "The managed table now points to a different source. Open a new request."
            )
        return cfg

    def _now_ns(self) -> int:
        value = scalar(self._sql, "SELECT DATE_PART(EPOCH_NANOSECOND, CURRENT_TIMESTAMP()) AS NS")
        return int(value)

    def _column_metadata(self, table: TableRef) -> list[Row]:
        return self._sql.query(
            "SELECT COLUMN_NAME, COLUMN_DEFAULT, DATA_TYPE, IS_NULLABLE, "
            "CHARACTER_MAXIMUM_LENGTH, CHARACTER_OCTET_LENGTH, NUMERIC_PRECISION, "
            "NUMERIC_SCALE, DATETIME_PRECISION, IS_IDENTITY, IDENTITY_GENERATION, "
            "IDENTITY_START, IDENTITY_INCREMENT "
            f"FROM {information_schema(table.database)}.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            [table.schema, table.name],
        )

    def _source_columns(self, cfg: TableConfig, table: TableRef | None = None) -> list[str]:
        rows = self._column_metadata(table or cfg.source)
        columns = [str(r["COLUMN_NAME"]) for r in rows]
        if not columns:
            raise WorkflowError(f"Source table {cfg.source} was not found or is not readable.")
        if any(has_sequence_default(r.get("COLUMN_DEFAULT")) for r in rows):
            raise WorkflowError(f"{cfg.source} has a sequence default shared by its clones.")
        if RESERVED_COLUMNS & set(columns):
            raise WorkflowError(f"Source columns may not be named {sorted(RESERVED_COLUMNS)}.")
        missing = set(cfg.key_columns) - set(columns)
        if missing:
            raise WorkflowError(f"Key columns {sorted(missing)} do not exist in {cfg.source}.")
        return columns

    def _check_tracking(self, ref: TableRef, snapshot_ns: int) -> None:
        retention = scalar(
            self._sql,
            f"SELECT RETENTION_TIME FROM {information_schema(ref.database)}.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [ref.schema, ref.name],
        )
        if not retention or int(retention) < 1:
            raise WorkflowError(f"{ref} needs at least one day of Time Travel retention.")
        # An empty, explicitly bounded interval checks the same CHANGES capability
        # that approval will need, before any sandbox DDL or user DML.
        try:
            self._sql.query(drift_counts_sql(ref.sql, snapshot_ns, snapshot_ns))
        except Exception as error:
            raise WorkflowError(
                f"Check change tracking and read access on {ref}: {error}"
            ) from error

    def _build_sandbox(
        self, cfg: TableConfig, clone: TableRef, stream: TableRef, label: str, snapshot_ns: int
    ) -> None:
        """Create a clone at the specified snapshot and a stream on it."""
        self._sql.query(
            f"CREATE OR REPLACE TABLE {clone.sql} CLONE {cfg.source.sql} "
            f"AT(TIMESTAMP => TO_TIMESTAMP_LTZ({snapshot_ns}, 9))"
        )
        # Clones inherit the source's comment; replace it so the object explains itself.
        self._sql.query(f"ALTER TABLE {clone.sql} SET COMMENT = {sql_string_literal(label)}")
        # The stream must exist before the DML so it captures exactly its effect.
        self._sql.query(f"CREATE OR REPLACE STREAM {stream.sql} ON TABLE {clone.sql}")

    def _run_dml(self, sql: str, attempt: _DmlAttempt | None = None) -> tuple[DiffCounts, str, int]:
        evidence = attempt if attempt is not None else _DmlAttempt()
        started = self._clock()
        try:
            rows = self._sql.query_dml(sql)
        except Exception as error:
            # A failed Snowpark statement carries its own ID, even if no counts were returned.
            evidence.query_id = getattr(error, "sfqid", None)
            raise
        finally:
            evidence.elapsed_ms = round((self._clock() - started) * 1000)
        query_id = str(scalar(self._sql, "SELECT LAST_QUERY_ID() AS QID") or "")
        evidence.query_id = query_id
        row = {label.lower(): value for label, value in rows[0].items()} if rows else {}
        evidence.counts = DiffCounts.from_dml_result(row)
        multi_joined = int(row.get("number of multi-joined rows updated", 0))
        if multi_joined > 0:
            raise _ExecutionCountError(
                f"UPDATE {query_id} matched multiple source rows for {multi_joined} target "
                "row(s). Make the join return a single source row per target row."
            )
        return evidence.counts, query_id, evidence.elapsed_ms

    def _sandboxed_run(
        self,
        cfg: TableConfig,
        guarded: GuardedStatement,
        names: tuple[TableRef, ...],
        label: str,
    ) -> dict[str, Any]:
        """Clone, stream, run the DML on the clone, materialize and measure its diff."""
        clone, stream, diff = names
        snapshot_ns = self._now_ns()
        for ref in sorted({cfg.source, *guarded.reads}, key=str):
            self._check_tracking(ref, snapshot_ns)
        self._build_sandbox(cfg, clone, stream, label, snapshot_ns)
        # Defaults belong to the cloned schema as of the snapshot. Even an INSERT
        # omitting a sequence-backed column could advance the original sequence.
        columns = self._source_columns(cfg, clone)
        counts, query_id, elapsed_ms = self._run_dml(
            guarded.render_for(clone, lookup_snapshot_ns=snapshot_ns)
        )
        self._sql.query(materialize_sql(diff.sql, stream.sql))
        diff_counts = DiffCounts.from_row(self._sql.query(counts_sql(diff.sql))[0])
        fingerprint = scalar(self._sql, fingerprint_sql(diff.sql, columns))
        return {
            "snapshot_ns": snapshot_ns,
            "counts": counts,
            "diff_counts": diff_counts,
            "fingerprint": None if fingerprint is None else str(fingerprint),
            "query_id": query_id,
            "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _names(request: Mapping[str, Any]) -> SandboxNames:
        return SandboxNames(base=str(request["SANDBOX_BASE"]))

    @staticmethod
    def _ref(request: Mapping[str, Any], name: str) -> TableRef:
        return TableRef(str(request["CLONE_DATABASE"]), str(request["CLONE_SCHEMA"]), name)

    def _transition(
        self,
        request: Mapping[str, Any],
        new: Status,
        values: Mapping[str, Any],
        expected_revision: int | None = None,
    ) -> None:
        current = ensure_transition(str(request["STATUS"]), new)
        updated = self._store.update(
            str(request["REQUEST_ID"]),
            {**values, "STATUS": str(new)},
            expected_status=current,
            expected_revision=expected_revision,
        )
        if updated != 1:
            raise WorkflowError("The request was changed by someone else; reload and retry.")

    def _record(
        self,
        request: Mapping[str, Any],
        new: Status | None,
        values: Mapping[str, Any],
        event: str,
        actor: str,
        *,
        details: Mapping[str, Any],
        expected_revision: int | None = None,
    ) -> None:
        """Apply a change and its audit event in one transaction: both or neither."""
        request_id = str(request["REQUEST_ID"])
        with self._transaction():
            if new is None:
                if (
                    self._store.update(
                        request_id,
                        values,
                        expected_status=str(request["STATUS"]),
                        expected_revision=expected_revision,
                    )
                    != 1
                ):
                    raise WorkflowError(
                        "The request was changed by someone else; reload and retry."
                    )
            else:
                self._transition(request, new, values, expected_revision)
            self._store.audit(request_id, event, actor, details)

    @staticmethod
    def _revision(request: Mapping[str, Any]) -> int:
        return int(request.get("REVISION") or 0)

    def _claim_run(self, request: Mapping[str, Any]) -> int:
        """Start a sandbox run: bump REVISION so any earlier or concurrent result is stale.

        Returns the new revision; the run's results are only stored if it is still
        current, and submit / approval require the latest *completed* run
        (``RUN_REVISION``) to be the current revision.
        """
        revision = self._revision(request) + 1
        with self._transaction():
            claimed = self._store.update(
                str(request["REQUEST_ID"]),
                {"REVISION": revision, "LAST_ERROR": None},
                expected_status=str(request["STATUS"]),
                expected_revision=revision - 1,
            )
            if claimed != 1:
                raise WorkflowError("The request was changed by someone else; reload and retry.")
        return revision

    @contextlib.contextmanager
    def _attempt(
        self,
        request: Mapping[str, Any],
        actor: str,
        revision_field: str,
        failure_event: str,
    ) -> Iterator[tuple[int, tuple[TableRef, ...]]]:
        revision = self._claim_run(request)
        names = self._names(request).for_revision(revision)
        refs = tuple(self._ref(request, n) for n in (names.clone, names.stream, names.diff))
        try:
            yield revision, refs
        except Exception as error:
            if self._rollback_uncertain:
                raise
            # A failed or overtaken run owns only its own provisional objects.
            # Keep the published result if a COMMIT acknowledgement was lost.
            with contextlib.suppress(Exception):
                if self._store.get(str(request["REQUEST_ID"])).get(revision_field) != revision:
                    with contextlib.suppress(Exception):
                        self._drop_attempt(request, names)
                    self._record(
                        request,
                        None,
                        {"LAST_ERROR": str(error)[:MAX_TEXT]},
                        failure_event,
                        actor,
                        details={"error": str(error)},
                        expected_revision=revision,
                    )
            raise

    def _check_displayed_revision(self, request: Mapping[str, Any], displayed: int | None) -> None:
        if displayed is not None and displayed != self._revision(request):
            raise WorkflowError(
                "The request has changed since it was displayed. Reload before continuing."
            )

    def _ensure_run_current(self, request: Mapping[str, Any], what: str) -> int:
        revision = self._revision(request)
        run_revision = request.get("RUN_REVISION")
        # Requests created before revisions existed have neither column set.
        if (0 if run_revision is None else int(run_revision)) != revision:
            raise WorkflowError(
                f"A sandbox run is in progress or did not finish, so there is nothing "
                f"consistent to {what}. Wait for it, or run it again."
            )
        return revision

    def _baseline(self, request: Mapping[str, Any]) -> tuple[int, TableRef]:
        """Snapshot and diff table of the most recent sandbox run (re-validation wins)."""
        names = self._names(request)
        if request.get("REVAL_SNAPSHOT_NS"):
            return int(request["REVAL_SNAPSHOT_NS"]), self._ref(
                request, names.published(request, "reval_diff")
            )
        if not request.get("DRY_RUN_AT") or not request.get("SNAPSHOT_NS"):
            raise WorkflowError("The request has not been dry-run yet.")
        return int(request["SNAPSHOT_NS"]), self._ref(request, names.published(request, "diff"))

    def _check_definition_time(self, table: TableRef, baseline_ns: int, *, lookup: bool) -> None:
        # Time Travel reads use current column definitions. CHANGES alone cannot
        # detect a column rename or a revised expression that changes the DML.
        timestamp = scalar(
            self._sql,
            "SELECT DATE_PART(EPOCH_NANOSECOND, COALESCE(LAST_DDL, CREATED)) AS DDL_NS "
            f"FROM {information_schema(table.database)}.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [table.schema, table.name],
        )
        last_ddl_ns = int(timestamp) if timestamp is not None else None
        if last_ddl_ns is not None and last_ddl_ns <= baseline_ns:
            return
        role = "lookup" if lookup else "target"
        if last_ddl_ns is None:
            message = f"The definition timestamp of the {role} table {table} is unavailable."
            reason = f"{role.upper()}_DDL_UNAVAILABLE"
        else:
            message = (
                f"The {role} table definition for {table} changed after the reviewed snapshot."
            )
            reason = f"{role.upper()}_DDL_CHANGED"
        raise _DefinitionChangedError(
            message,
            {
                "reason": reason,
                "table_fqn": str(table),
                "baseline_ns": baseline_ns,
                "last_ddl_ns": last_ddl_ns,
            },
        )

    def _check_review_baseline(self, request: Mapping[str, Any], cfg: TableConfig) -> DriftReport:
        if request.get("RUN_CONTEXT_VERSION") != DML_CONTEXT_VERSION:
            raise _RevalidationRequiredError(
                "The last sandbox run used earlier SQL session settings.",
                {"reason": "RUN_CONTEXT_CHANGED", "required_version": DML_CONTEXT_VERSION},
            )
        # CHANGES tracks row DML. A schema-only change can alter the values an
        # approved INSERT will write without affecting its row count.
        kind = "reval_clone" if request.get("REVAL_SNAPSHOT_NS") else "clone"
        clone = self._ref(request, self._names(request).published(request, kind))
        reviewed_columns = self._column_metadata(clone)
        if not reviewed_columns or reviewed_columns != self._column_metadata(cfg.source):
            raise _DefinitionChangedError(
                f"The schema of {cfg.source} no longer matches the reviewed clone.",
                {"reason": "SCHEMA_CHANGED", "source": str(cfg.source)},
            )
        try:
            report = self._drift(request, cfg)
        except Exception as error:
            message = str(getattr(error, "raw_message", None) or error)
            code = str(getattr(error, "sql_error_code", None) or "").lstrip("0")
            if code == "707" or (
                not code and "time travel data is not available for table" in message.casefold()
            ):
                reason = "Time Travel history is unavailable at the reviewed snapshot."
                category = "HISTORY_UNAVAILABLE"
            elif code == "91930" or (
                not code
                and "change tracking is not enabled or has been missing" in message.casefold()
            ):
                reason = (
                    "Change tracking is unavailable for the reviewed interval. Ask the table "
                    "owner to verify change tracking on the target and each referenced lookup."
                )
                category = "CHANGE_TRACKING_UNAVAILABLE"
            else:
                raise
            raise _RevalidationRequiredError(
                reason,
                {"reason": category, "error": message},
            ) from error
        if report.total:
            raise _RevalidationRequiredError(
                f"The source changed since the last sandbox run ({report.total} rows, "
                f"{report.overlapping_keys} overlapping).",
                report.as_dict(),
            )
        return report

    def _drift(self, request: Mapping[str, Any], cfg: TableConfig) -> DriftReport:
        baseline_ns, diff = self._baseline(request)
        self._check_definition_time(cfg.source, baseline_ns, lookup=False)
        end_ns = self._now_ns()
        counts = self._sql.query(drift_counts_sql(cfg.source.sql, baseline_ns, end_ns))[0]
        overlap = self._sql.query(
            drift_overlap_sql(cfg.source.sql, baseline_ns, diff.sql, cfg.key_columns, end_ns)
        )[0]
        # Tables the statement reads (MERGE ... USING, INSERT ... SELECT) change its
        # effect too, so changes to them count as drift as well.
        read_changes = 0
        if request.get("CANONICAL_SQL"):
            guarded = guard_dml(str(request["CANONICAL_SQL"]), cfg.source, cfg.allowed_reads)
            for ref in sorted(guarded.reads, key=str):
                self._check_definition_time(ref, baseline_ns, lookup=True)
                row = self._sql.query(drift_counts_sql(ref.sql, baseline_ns, end_ns))[0]
                read_changes += sum(
                    int(row.get(k) or 0) for k in ("INSERTED", "UPDATED", "DELETED")
                )
        return build_report(
            baseline_ns, counts, overlap, read_changes, end_ns=end_ns, diff_fqn=str(diff)
        )

    # --------------------------------------------------------------- operations
    def create_request(self, table_id: str, actor: str) -> dict[str, Any]:
        """Open a DRAFT request and clone the source into the sandbox."""
        cfg = self._config(table_id)
        self._source_columns(cfg)
        snapshot_ns = self._now_ns()
        self._check_tracking(cfg.source, snapshot_ns)
        request_id = self._new_id()
        names = build_sandbox_names(cfg.source.name, actor, request_id, datetime.now(UTC))
        with self._transaction():
            self._store.insert(
                {
                    "REQUEST_ID": request_id,
                    "TABLE_ID": cfg.table_id,
                    "SOURCE_FQN": str(cfg.source),
                    "STATUS": str(Status.DRAFT),
                    "REQUESTED_BY": actor,
                    "CLONE_DATABASE": cfg.clone_database,
                    "CLONE_SCHEMA": cfg.clone_schema,
                    "SANDBOX_BASE": names.base,
                    "SNAPSHOT_NS": snapshot_ns,
                    "REVISION": 0,
                    "RUN_REVISION": -1,
                    "CREATED_AT": NOW,
                    "UPDATED_AT": NOW,
                }
            )
            self._store.audit(request_id, "CREATED", actor, {"clone": names.clone})
        # Register the names before doing DDL, so partial creation is discoverable.
        # All subsequent sandbox attempts have their own names and snapshot.
        try:
            self._build_sandbox(
                cfg,
                cfg.sandbox_ref(names.clone),
                cfg.sandbox_ref(names.stream),
                _sandbox_label(request_id, actor, cfg.source, "dry-run sandbox"),
                snapshot_ns,
            )
        except Exception as error:
            try:
                with contextlib.suppress(WorkflowError):
                    self._record(
                        {"REQUEST_ID": request_id, "STATUS": str(Status.DRAFT)},
                        Status.CANCELLED,
                        {"CANCELLED_AT": NOW, "LAST_ERROR": str(error)[:MAX_TEXT]},
                        "SANDBOX_CREATION_FAILED",
                        actor,
                        details={"error": str(error)},
                        expected_revision=0,
                    )
            finally:
                # A concurrent cancellation and cleanup can finish before this
                # attempt creates its table. Discard any partial DDL from this call.
                if not self._rollback_uncertain:
                    self._discard_initial_sandbox(request_id, names, actor)
            raise
        with self._transaction():
            finished = self._store.update(request_id, {"RUN_REVISION": 0}, str(Status.DRAFT), 0)
        if not finished:
            current = self._store.get(request_id)
            if current["STATUS"] == Status.CANCELLED or current.get("SANDBOX_DROPPED_AT"):
                self._discard_initial_sandbox(request_id, names, actor)
                raise WorkflowError(
                    f"Request {request_id} is now {current['STATUS']}. "
                    "Its initial sandbox is no longer available."
                )
        return {"request_id": request_id, "clone": str(cfg.sandbox_ref(names.clone))}

    def dry_run(
        self, request_id: str, actor: str, sql_text: str, nl_prompt: str | None = None
    ) -> dict[str, Any]:
        """Validate the SQL, run it on a fresh clone and record exactly what changed."""
        request = self._store.get(request_id)
        ensure_requester(str(request["REQUESTED_BY"]), actor, "dry-run")
        if Status(str(request["STATUS"])) not in EDITABLE_STATUSES:
            raise WorkflowError("Only draft requests can be dry-run; create a new request.")
        cfg = self._request_config(request)
        sql_text = _clean_text(sql_text, "SQL", required=True)
        prompt = _clean_text(nl_prompt, "Prompt") or None
        guarded = guard_dml(sql_text, cfg.source, cfg.allowed_reads)
        with self._attempt(request, actor, "DRY_SANDBOX_REVISION", "DRY_RUN_FAILED") as (
            revision,
            refs,
        ):
            run = self._sandboxed_run(
                cfg, guarded, refs, _sandbox_label(request_id, actor, cfg.source, "dry-run sandbox")
            )
            values = {
                "SQL_TEXT": sql_text,
                "CANONICAL_SQL": guarded.canonical_sql,
                "SQL_SHA256": sha256(guarded.canonical_sql),
                "STATEMENT_KIND": guarded.kind,
                "NL_PROMPT": prompt,
                "SNAPSHOT_NS": run["snapshot_ns"],
                "DRY_RUN_COUNTS": run["counts"].as_dict(),
                "DRY_RUN_DIFF_COUNTS": run["diff_counts"].as_dict(),
                "DRY_RUN_FINGERPRINT": run["fingerprint"],
                "DRY_RUN_QUERY_ID": run["query_id"],
                "DRY_RUN_ELAPSED_MS": run["elapsed_ms"],
                "DRY_RUN_AT": NOW,
                "LAST_ERROR": None,
                "RUN_CONTEXT_VERSION": DML_CONTEXT_VERSION,
                "RUN_REVISION": revision,
                "DRY_SANDBOX_REVISION": revision,
            }
            result = {
                "request_id": request_id,
                "kind": guarded.kind,
                "counts": run["counts"].as_dict(),
                "diff_counts": run["diff_counts"].as_dict(),
                "elapsed_ms": run["elapsed_ms"],
                "query_id": run["query_id"],
                "max_affected_rows": cfg.max_affected_rows,
            }
            self._record(
                request,
                Status.DRY_RUN_OK,
                values,
                "DRY_RUN_OK",
                actor,
                details=result,
                expected_revision=revision,
            )
            return result

    def submit(
        self,
        request_id: str,
        actor: str,
        note: str | None = None,
        sql: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Send a successfully dry-run request to the admins.

        ``sql`` is the statement the requester is looking at; when given it must be
        the one that was dry-run, so a later edit cannot be submitted unseen.
        """
        request = self._store.get(request_id)
        ensure_requester(str(request["REQUESTED_BY"]), actor, "submit")
        self._check_displayed_revision(request, expected_revision)
        if sql is not None and sql.strip() != str(request.get("SQL_TEXT") or "").strip():
            raise WorkflowError(
                "The SQL was edited after the last dry run. "
                "Run the dry run again before submitting."
            )
        cfg = self._request_config(request)
        counts = _counts(request.get("DRY_RUN_COUNTS"))
        if counts.total == 0:
            raise WorkflowError("The dry run affected no rows; there is nothing to submit.")
        if counts.total > cfg.max_affected_rows:
            raise WorkflowError(
                f"The dry run affected {counts.total} rows, above the limit of "
                f"{cfg.max_affected_rows} for {cfg.table_id}."
            )
        if sha256(str(request["CANONICAL_SQL"])) != request["SQL_SHA256"]:
            raise WorkflowError("Stored SQL failed its integrity check.")
        guard_dml(str(request["CANONICAL_SQL"]), cfg.source, cfg.allowed_reads)
        revision = self._ensure_run_current(request, "submit")
        self._record(
            request,
            Status.SUBMITTED,
            {"SUBMITTED_AT": NOW, "SUBMIT_NOTE": _clean_text(note, "Note") or None},
            "SUBMITTED",
            actor,
            details={"counts": counts.as_dict()},
            expected_revision=revision,
        )
        return {"request_id": request_id, "status": str(Status.SUBMITTED)}

    def cancel(self, request_id: str, actor: str, reason: str | None = None) -> dict[str, Any]:
        """Withdraw a request that has not started executing."""
        request = self._store.get(request_id)
        ensure_requester(str(request["REQUESTED_BY"]), actor, "cancel")
        self._record(
            request,
            Status.CANCELLED,
            {"CANCELLED_AT": NOW},
            "CANCELLED",
            actor,
            details={"reason": _clean_text(reason, "Reason")},
        )
        return {"request_id": request_id, "status": str(Status.CANCELLED)}

    def send_back(
        self, request_id: str, actor: str, comment: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Admin: return an approved, not yet executed request to review."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "send back")
        self._check_displayed_revision(request, expected_revision)
        if str(request["STATUS"]) != Status.APPROVED:
            raise WorkflowError("Only approved requests can be sent back for review.")
        comment_text = _clean_text(comment, "A comment", required=True)
        self._return_to_review(
            request,
            actor,
            f"Sent back by {actor}: {comment_text}",
            "SENT_BACK",
            {"comment": comment_text},
        )
        return {"request_id": request_id, "status": str(Status.SUBMITTED)}

    def _return_to_review(
        self,
        request: Mapping[str, Any],
        actor: str,
        reason: str,
        event: str,
        details: Mapping[str, Any],
        *,
        execution_values: Mapping[str, Any] | None = None,
    ) -> None:
        # Invalidate stale execution attempts even if another admin re-approves
        # with a different tolerance while the first screen or call is still open.
        revision = self._revision(request)
        self._record(
            request,
            Status.SUBMITTED,
            {
                **(execution_values or {}),
                "APPROVED_SHA256": None,
                "LAST_ERROR": reason,
                "REVISION": revision + 1,
                "RUN_REVISION": revision + 1,
            },
            event,
            actor,
            details=details,
            expected_revision=revision,
        )

    def drift(self, request_id: str, requester: str | None = None) -> dict[str, Any]:
        """Report changes to the source since the latest dry run / re-validation."""
        request = self._store.get(request_id)
        if requester is not None:
            ensure_requester(str(request["REQUESTED_BY"]), requester, "inspect drift for")
        cfg = self._request_config(request)
        report = self._drift(request, cfg)
        return {
            **report.as_dict(),
            "revision": self._revision(request),
            "source_fqn": str(cfg.source),
            "key_columns": list(cfg.key_columns),
            "revalidated": bool(request.get("REVAL_SNAPSHOT_NS")),
        }

    def revalidate(self, request_id: str, actor: str) -> dict[str, Any]:
        """Admin: re-run the submitted SQL on a fresh clone of the *current* source."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "re-validate")
        if str(request["STATUS"]) != Status.SUBMITTED:
            raise WorkflowError("Only submitted requests can be re-validated.")
        cfg = self._request_config(request)
        if sha256(str(request["CANONICAL_SQL"])) != request["SQL_SHA256"]:
            raise WorkflowError("Stored SQL failed its integrity check.")
        legacy = request.get("RUN_CONTEXT_VERSION") != DML_CONTEXT_VERSION
        guidance = "Ask the requester to open a new request and dry-run the original SQL."
        try:
            guarded = guard_dml(str(request["CANONICAL_SQL"]), cfg.source, cfg.allowed_reads)
            if legacy:
                original = guard_dml(
                    str(request.get("SQL_TEXT") or ""), cfg.source, cfg.allowed_reads
                )
                if original.canonical_sql != guarded.canonical_sql:
                    raise WorkflowError(
                        f"The original SQL now differs from the submitted statement. {guidance}"
                    )
        except GuardError as error:
            if legacy:
                raise WorkflowError(
                    f"The original developer SQL cannot be verified. {guidance}"
                ) from error
            raise
        with self._attempt(request, actor, "REVAL_SANDBOX_REVISION", "REVALIDATION_FAILED") as (
            revision,
            refs,
        ):
            run = self._sandboxed_run(
                cfg, guarded, refs, _sandbox_label(request_id, actor, cfg.source, "re-validation")
            )
            dev_counts = _counts(request.get("DRY_RUN_COUNTS"))
            matches = (
                run["counts"] == dev_counts
                and run["fingerprint"] is not None
                and run["fingerprint"] == request.get("DRY_RUN_FINGERPRINT")
            )
            result = {
                "request_id": request_id,
                "counts": run["counts"].as_dict(),
                "dry_run_counts": dev_counts.as_dict(),
                "same_effect": matches,
                "revision": revision,
                "query_id": run["query_id"],
                "elapsed_ms": run["elapsed_ms"],
            }
            self._record(
                request,
                None,
                {
                    "REVAL_SNAPSHOT_NS": run["snapshot_ns"],
                    "REVAL_COUNTS": run["counts"].as_dict(),
                    "REVAL_FINGERPRINT": run["fingerprint"],
                    "REVAL_MATCHES": matches,
                    "REVAL_BY": actor,
                    "REVAL_AT": NOW,
                    "RUN_CONTEXT_VERSION": DML_CONTEXT_VERSION,
                    "RUN_REVISION": revision,
                    "REVAL_SANDBOX_REVISION": revision,
                    "LAST_ERROR": None,
                },
                "REVALIDATED",
                actor,
                details=result,
                expected_revision=revision,
            )
            return result

    def review(
        self,
        request_id: str,
        actor: str,
        decision: str,
        comment: str,
        tolerance_pct: float = 0.0,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Admin: approve or reject. Approval requires no drift since the latest sandbox run."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "review")
        self._check_displayed_revision(request, expected_revision)
        comment_text = _clean_text(comment, "A review comment", required=True)
        decision = decision.upper()
        if decision == "REJECT":
            self._record(
                request,
                Status.REJECTED,
                {"REVIEWED_BY": actor, "REVIEWED_AT": NOW, "REVIEW_COMMENT": comment_text},
                "REJECTED",
                actor,
                details={"comment": comment_text},
                expected_revision=self._revision(request),
            )
            return {"request_id": request_id, "status": str(Status.REJECTED)}
        if decision != "APPROVE":
            raise WorkflowError("decision must be APPROVE or REJECT.")
        if not 0 <= float(tolerance_pct) <= 100:
            raise WorkflowError("tolerance_pct must be between 0 and 100.")
        cfg = self._request_config(request)
        for column in ("DRY_RUN_COUNTS", "REVAL_COUNTS"):
            total = _counts(request.get(column)).total
            if total > cfg.max_affected_rows:
                raise WorkflowError(
                    f"{total} affected rows exceed the limit of {cfg.max_affected_rows} "
                    f"for {cfg.table_id}."
                )
        revision = self._ensure_run_current(request, "approve")
        if sha256(str(request["CANONICAL_SQL"])) != request["SQL_SHA256"]:
            raise WorkflowError("Stored SQL failed its integrity check.")
        try:
            report = self._check_review_baseline(request, cfg)
        except _DefinitionChangedError as error:
            if request["STATUS"] == Status.SUBMITTED:
                self._record(
                    request,
                    None,
                    {"LAST_ERROR": error.reason},
                    "APPROVAL_BLOCKED",
                    actor,
                    details=error.details,
                    expected_revision=revision,
                )
            raise
        self._record(
            request,
            Status.APPROVED,
            {
                "REVIEWED_BY": actor,
                "REVIEWED_AT": NOW,
                "REVIEW_COMMENT": comment_text,
                "APPROVED_SHA256": request["SQL_SHA256"],
                "TOLERANCE_PCT": float(tolerance_pct),
            },
            "APPROVED",
            actor,
            details={"comment": comment_text, "drift": report.as_dict()},
            expected_revision=revision,
        )
        return {"request_id": request_id, "status": str(Status.APPROVED)}

    def execute(
        self, request_id: str, actor: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Admin: run the approved statement on the source, transactionally and guarded."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "execute")
        self._check_displayed_revision(request, expected_revision)
        if str(request["STATUS"]) != Status.APPROVED:
            raise WorkflowError("Only approved requests can be executed.")
        cfg = self._request_config(request)
        canonical = str(request["CANONICAL_SQL"])
        if sha256(canonical) != request.get("APPROVED_SHA256"):
            raise WorkflowError("The SQL no longer matches what was approved.")
        guarded = guard_dml(canonical, cfg.source, cfg.allowed_reads)

        revision = self._ensure_run_current(request, "execute")
        expected = _counts(request.get("REVAL_COUNTS") or request.get("DRY_RUN_COUNTS"))
        tolerance = float(request.get("TOLERANCE_PCT") or 0.0)
        attempt = _DmlAttempt()
        result: Row | None = None
        claimed = False
        self._store.begin()
        try:
            self._transition(request, Status.EXECUTING, {"EXECUTED_BY": actor}, revision)
            claimed = True
            self._store.audit(request_id, "EXECUTING", actor, {})
            self._check_review_baseline(request, cfg)
            self._source_columns(cfg)
            actual, query_id, elapsed_ms = self._run_dml(guarded.render_for(cfg.source), attempt)
            if not within_tolerance(expected, actual, tolerance):
                raise _ExecutionCountError(
                    f"Rolled back: affected {actual.as_dict()} but approval expected "
                    f"{expected.as_dict()} (tolerance {tolerance}%)."
                )
            if actual.total > cfg.max_affected_rows:
                raise _ExecutionCountError(
                    f"Rolled back: affected {actual.total} rows, above the limit of "
                    f"{cfg.max_affected_rows} for {cfg.table_id}."
                )
            # Read committed allows another writer to finish between a preflight
            # query and the source DML. Check committed history through a timestamp
            # after the DML, while the source mutation can still be rolled back.
            self._check_review_baseline(request, cfg)
            result = {
                "request_id": request_id,
                "status": str(Status.EXECUTED),
                "counts": actual.as_dict(),
                "query_id": query_id,
                "elapsed_ms": elapsed_ms,
            }
            self._transition(
                {**request, "STATUS": str(Status.EXECUTING)},
                Status.EXECUTED,
                {
                    "EXEC_COUNTS": actual.as_dict(),
                    "EXEC_QUERY_ID": query_id,
                    "EXEC_ELAPSED_MS": elapsed_ms,
                    "EXECUTED_AT": NOW,
                },
                revision,
            )
            self._store.audit(request_id, "EXECUTED", actor, result)
            self._store.commit()
            return result
        except Exception as error:
            self._rollback(error)
            if result is not None:
                current = self._store.get(request_id)
                if (
                    current["STATUS"] == Status.EXECUTED
                    and current.get("EXEC_QUERY_ID") == result["query_id"]
                ):
                    return result  # The COMMIT succeeded and only its response was lost.
            if claimed:
                self._record_execution_error(request, actor, error, attempt, revision)
                if isinstance(error, _RevalidationRequiredError):
                    raise WorkflowError(
                        f"{error.reason} The request went back to SUBMITTED for re-validation."
                    ) from error
            raise

    def _record_execution_error(
        self,
        request: Mapping[str, Any],
        actor: str,
        error: Exception,
        attempt: _DmlAttempt,
        revision: int,
    ) -> None:
        evidence = attempt.details()
        exec_values = {
            "EXEC_QUERY_ID": attempt.query_id or None,
            "EXEC_ELAPSED_MS": attempt.elapsed_ms,
            "EXEC_COUNTS": evidence.get("counts"),
        }
        if isinstance(error, _RevalidationRequiredError):
            self._return_to_review(
                request,
                actor,
                error.reason,
                "DRIFT_BLOCKED",
                {**error.details, **evidence},
                execution_values=exec_values,
            )
            return
        values: Row = {
            **exec_values,
            "LAST_ERROR": str(error)[:MAX_TEXT],
            "EXECUTED_AT": NOW,
            "EXECUTED_BY": actor,
        }
        self._record(
            request,
            Status.FAILED,
            values,
            "EXECUTION_ROLLED_BACK"
            if isinstance(error, _ExecutionCountError)
            else "EXECUTION_FAILED",
            actor,
            details={"error": str(error), **evidence},
            expected_revision=revision,
        )

    def _drop_attempt(self, request: Mapping[str, Any], names: SandboxNames) -> None:
        self._sql.query(f"DROP STREAM IF EXISTS {self._ref(request, names.stream).sql}")
        for table in (names.clone, names.diff):
            self._sql.query(f"DROP TABLE IF EXISTS {self._ref(request, table).sql}")

    def _discard_initial_sandbox(self, request_id: str, names: SandboxNames, actor: str) -> None:
        request = self._store.get(request_id)
        try:
            self._drop_attempt(request, names)
        except Exception as error:
            details = {"error": str(error), "clone": names.clone}
            # A cleanup that already visited the base might still be marking it
            # dropped. Invalidate that claim and make the terminal row retryable.
            while request["STATUS"] in TERMINAL_STATUSES:
                revision = self._revision(request)
                with self._transaction():
                    if self._store.update(
                        request_id,
                        {"SANDBOX_DROPPED_AT": None, "REVISION": revision + 1},
                        str(request["STATUS"]),
                        revision,
                    ):
                        self._store.audit(request_id, "SANDBOX_DROP_FAILED", actor, details)
                        return
                request = self._store.get(request_id)
            self._store.audit(request_id, "SANDBOX_DROP_FAILED", actor, details)

    def _expire_idle_drafts(self, days: int, actor: str, request_id: str | None) -> list[Row]:
        expired = []
        for request in self._store.idle_drafts(days, request_id):
            try:
                self._record(
                    request,
                    Status.CANCELLED,
                    {"CANCELLED_AT": NOW, "LAST_ERROR": f"Draft expired after {days} idle days."},
                    "DRAFT_EXPIRED",
                    actor,
                    details={"idle_days": days},
                    expected_revision=self._revision(request),
                )
            except WorkflowError:
                if self._rollback_uncertain:
                    raise
                continue  # A user edited or submitted it after the candidate query.
            expired.append({**request, "STATUS": str(Status.CANCELLED)})
        return expired

    def cleanup(
        self,
        retention_days: int,
        actor: str,
        request_id: str | None = None,
        idle_draft_days: int = 0,
    ) -> dict[str, Any]:
        """Drop sandbox objects of final requests older than ``retention_days``.

        One request whose objects cannot be dropped is audited and skipped, so it
        does not block the cleanup of every other request night after night.
        """
        if min(retention_days, idle_draft_days) < 0:
            raise WorkflowError("Cleanup retention and idle draft days must be 0 or more.")
        dropped, failed = 0, 0
        candidates = self._store.cleanup_candidates(retention_days, request_id)
        expired = (
            self._expire_idle_drafts(idle_draft_days, actor, request_id) if idle_draft_days else []
        )
        for request in [*candidates, *expired]:
            request_id = str(request["REQUEST_ID"])
            names = self._names(request)
            revision = self._revision(request)
            try:
                self._drop_attempt(request, names)
                # Legacy re-validations used the unversioned _RV triple.
                self._drop_attempt(request, SandboxNames(names.reval_clone))
                for attempt in range(1, revision + 1):
                    self._drop_attempt(request, names.for_revision(attempt))
                self._record(
                    request,
                    None,
                    {"SANDBOX_DROPPED_AT": NOW, "REVISION": revision + 1},
                    "SANDBOX_DROPPED",
                    actor,
                    details={},
                    expected_revision=revision,
                )
                dropped += 1
            except Exception as error:  # Report per request and continue.
                if self._rollback_uncertain:
                    raise
                with contextlib.suppress(Exception):
                    if self._store.get(request_id).get("SANDBOX_DROPPED_AT"):
                        continue  # A concurrent cleanup finished first.
                failed += 1
                with contextlib.suppress(Exception):
                    self._store.audit(
                        request_id, "SANDBOX_DROP_FAILED", actor, {"error": str(error)}
                    )
        return {"dropped": dropped, "failed": failed, "expired": len(expired)}


#: Operations of ``CORE.SP_DEV_API`` (developer app) and ``CORE.SP_ADMIN_API``
#: (admin app and the cleanup task). Only ``drift`` (read-only) is in both.
DEVELOPER_OPS = frozenset({"create_request", "dry_run", "submit", "cancel", "drift"})
ADMIN_OPS = frozenset({"drift", "revalidate", "review", "execute", "send_back", "cleanup"})


def _actor(args: Mapping[str, Any]) -> str:
    actor = _clean_text(args.get("actor"), "actor", required=True)
    if len(actor) > 255:
        raise WorkflowError("actor is too long.")
    return actor


def dispatch(
    runner: DmlRunner,
    core_schema: str,
    op: str,
    raw_args: Any,
    allowed_ops: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Entry point of the ``CORE.SP_*_API(op, args)`` procedures; never raises.

    ``allowed_ops`` is the operation set of the calling procedure, so the developer
    app's procedure cannot review or execute even if its caller is wrong.
    """
    try:
        if allowed_ops is not None and op not in allowed_ops:
            raise WorkflowError(f"Operation {op!r} is not available through this procedure.")
        args: Row = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        engine = Engine(runner, SqlRequestStore(runner, core_schema))
        actor = _actor(args)
        rid = str(args.get("request_id") or "")
        operations: dict[str, Callable[[], dict[str, Any]]] = {
            "create_request": lambda: engine.create_request(str(args.get("table_id")), actor),
            "dry_run": lambda: engine.dry_run(
                rid, actor, str(args.get("sql") or ""), args.get("nl_prompt")
            ),
            "submit": lambda: engine.submit(
                rid, actor, args.get("note"), args.get("sql"), args.get("expected_revision")
            ),
            "cancel": lambda: engine.cancel(rid, actor, args.get("reason")),
            "drift": lambda: engine.drift(
                rid, requester=actor if allowed_ops == DEVELOPER_OPS else None
            ),
            "revalidate": lambda: engine.revalidate(rid, actor),
            "review": lambda: engine.review(
                rid,
                actor,
                str(args.get("decision") or ""),
                str(args.get("comment") or ""),
                _number(args, "tolerance_pct", 0.0),
                expected_revision=args.get("expected_revision"),
            ),
            "execute": lambda: engine.execute(rid, actor, args.get("expected_revision")),
            "send_back": lambda: engine.send_back(
                rid,
                actor,
                str(args.get("comment") or ""),
                expected_revision=args.get("expected_revision"),
            ),
            "cleanup": lambda: engine.cleanup(
                _days(args, "retention_days", 7),
                actor,
                _clean_text(args["request_id"], "request_id", required=True)
                if "request_id" in args
                else None,
                _days(args, "idle_draft_days", 0),
            ),
        }
        if op not in operations:
            raise WorkflowError(f"Unknown operation {op!r}.")
        return {"ok": True, "result": operations[op]()}
    except Exception as error:  # surfaced to the UI; the audit log keeps the details
        return {"ok": False, "error": str(error), "error_type": type(error).__name__}

# --------------------------------------------------------------------- handler
_CORE_SCHEMA = '"{{ app_db }}"."CORE"'


def handler(session, op, args):
    """Stored-procedure handler: dispatch one developer operation with owner's rights."""
    return dispatch(SnowparkRunner(session), _CORE_SCHEMA, op, args, DEVELOPER_OPS)
$$;

DEFINE PROCEDURE {{ app_db }}.CORE.SP_ADMIN_API(OP VARCHAR, ARGS VARIANT)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'sqlglot==30.17.0')
HANDLER = 'handler'
COMMENT = 'Auditable CRUD admin API (owner''s rights): cleanup, drift, execute, revalidate, review, send_back'
EXECUTE AS OWNER
AS
$$
from __future__ import annotations

# ==================================================================
# auditable_crud/naming.py
# ==================================================================
"""Identifier handling and sandbox object naming.

Every identifier that ends up in dynamically built SQL goes through this module:
names are validated or sanitized, then always emitted double-quoted, so no user
supplied text can break out of an identifier position.
"""


import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_IDENTIFIER_LENGTH = 255
_UNQUOTED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_NON_IDENT_CHARS = re.compile(r"[^A-Z0-9_]+")
# Longest suffix appended to a sandbox base name (see SandboxNames).
_LONGEST_SUFFIX = len("_RV_STRM")
_SANDBOX_OBJECTS = frozenset(
    {"clone", "stream", "diff", "reval_clone", "reval_stream", "reval_diff"}
)


class IdentifierError(ValueError):
    """Raised when a configured identifier is not a safe Snowflake identifier."""


def normalize_identifier(value: str) -> str:
    """Return the canonical (upper-case) form of an unquoted Snowflake identifier.

    Configuration values are restricted to plain unquoted identifiers, which
    Snowflake resolves case-insensitively as upper case.
    """
    stripped = value.strip()
    if not _UNQUOTED_IDENTIFIER.match(stripped) or len(stripped) > MAX_IDENTIFIER_LENGTH:
        raise IdentifierError(f"Not a valid unquoted Snowflake identifier: {value!r}")
    return stripped.upper()


def quote_identifier(name: str) -> str:
    """Quote an identifier exactly as given, escaping embedded double quotes."""
    if not name:
        raise IdentifierError("Identifier must not be empty")
    return '"' + name.replace('"', '""') + '"'


def fqn(database: str, schema: str, name: str) -> str:
    """Build a fully qualified, fully quoted three-part name."""
    return ".".join(quote_identifier(part) for part in (database, schema, name))


def information_schema(database: str) -> str:
    """Quoted ``"DB".INFORMATION_SCHEMA`` prefix for metadata queries."""
    return f"{quote_identifier(database)}.INFORMATION_SCHEMA"


def sql_string_literal(value: str) -> str:
    """Render ``value`` as a Snowflake single-quoted string literal.

    Only for DDL positions that do not accept bind variables (e.g. ``COMMENT``);
    everywhere else values are bound. Backslash is Snowflake's escape character
    inside single quotes, so it is doubled along with the quote itself.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sanitize_name_part(value: str, max_length: int) -> str:
    """Turn arbitrary text (e.g. a user name) into an identifier fragment."""
    cleaned = _NON_IDENT_CHARS.sub("_", value.upper()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned) or "X"
    return cleaned[:max_length].rstrip("_") or "X"


@dataclass(frozen=True)
class SandboxNames:
    """Unquoted object names for one change request's sandbox objects.

    ``base`` identifies the request: source table, timestamp, user and the
    request id, so a clone can be traced back to its owner just from its name.
    """

    base: str

    def for_revision(self, revision: int) -> SandboxNames:
        """Return distinct object names for one immutable run of this request."""
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision < 10**38
        ):
            raise ValueError("Sandbox revision must be a non-negative Snowflake integer.")
        if revision == 0:
            return self
        suffix = f"_R{revision}"
        room = MAX_IDENTIFIER_LENGTH - _LONGEST_SUFFIX - len(suffix)
        base = self.base
        if len(base) > room:
            # Retain the beginning of the table name and the timestamp, actor and
            # request ID at the end. The hash distinguishes legacy names which
            # differ only in the portion of the table name that was shortened.
            prefix = base[:16] + "_" + hashlib.sha256(base.encode()).hexdigest()[:16].upper() + "_"
            base = prefix + base[-(room - len(prefix)) :]
        return SandboxNames(base + suffix)

    def published(self, request: Mapping[str, Any], kind: str) -> str:
        """Resolve a request's published artifact, including pre-revision object names."""
        if kind not in _SANDBOX_OBJECTS:
            raise ValueError(f"Unknown sandbox object kind {kind!r}")
        field = "REVAL_SANDBOX_REVISION" if kind.startswith("reval_") else "DRY_SANDBOX_REVISION"
        revision = int(request.get(field) or 0)
        attribute = kind.removeprefix("reval_") if revision else kind
        return str(getattr(self.for_revision(revision), attribute))

    @property
    def clone(self) -> str:
        """Clone of the source table that the developer's DML runs against."""
        return self.base

    @property
    def stream(self) -> str:
        """Stream on the clone capturing exactly the rows the DML touched."""
        return f"{self.base}_STRM"

    @property
    def diff(self) -> str:
        """Materialized copy of the stream contents (stable, browsable)."""
        return f"{self.base}_DIFF"

    @property
    def reval_clone(self) -> str:
        """Clone used by the admin's re-validation against current data."""
        return f"{self.base}_RV"

    @property
    def reval_stream(self) -> str:
        """Stream on the re-validation clone."""
        return f"{self.base}_RV_STRM"

    @property
    def reval_diff(self) -> str:
        """Materialized diff of the re-validation run."""
        return f"{self.base}_RV_DIFF"


def build_sandbox_names(
    source_table: str, user_name: str, request_id: str, created_at: datetime
) -> SandboxNames:
    """Derive unique, identifiable sandbox names for a change request.

    Format: ``<SOURCE>__CR_<yyyymmddHHMMSS>_<USER>_<request UUID hex>``. The source part is
    truncated first so every derived name stays within Snowflake's 255-char limit.
    """
    stamp = created_at.strftime("%Y%m%d%H%M%S")
    user = sanitize_name_part(user_name, 40)
    request_token = sanitize_name_part(request_id.replace("-", ""), 32)
    tail = f"__CR_{stamp}_{user}_{request_token}"
    room = MAX_IDENTIFIER_LENGTH - _LONGEST_SUFFIX - len(tail)
    source = sanitize_name_part(source_table, room)
    return SandboxNames(base=f"{source}{tail}")

# ==================================================================
# auditable_crud/sql_guard.py
# ==================================================================
"""Static validation and target rewriting of user-supplied DML.

The guard is the security boundary between free-form SQL typed (or generated)
in the app and statements executed by an owner's-rights procedure. It runs in
the app for fast feedback and again inside the procedure before every
execution; only the procedure-side check is authoritative.

Rules enforced:

* exactly one statement, of type INSERT, UPDATE, DELETE or MERGE;
* the written table is the configured managed table;
* every other table read is either the managed table or explicitly allowed;
* reads use the current version of each physical table;
* sampling, row limits and operations that choose arbitrary rows are rejected;
  clock values are restricted to scalar assignments and explicit inserted values;
* no DDL, ``CALL``, ``EXECUTE IMMEDIATE``, session commands, table functions,
  ``INSERT OVERWRITE``/multi-table inserts, or functions outside the reviewed
  Snowflake built-ins.

Rewriting is done on the AST (never by string replacement) and every table
reference is emitted fully qualified, so the statement means the same thing
regardless of the session's current database and schema.
"""


import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError


MAX_SQL_LENGTH = 100_000
DIALECT = "snowflake"

# SQLGlot's typed function registry spans many SQL dialects. Check the emitted
# operation even when the parser recognized the name. These are SQLGlot node
# types for the Snowflake expressions used for filtering and transforming data.
_ALLOWED_FUNCTION_NODES: frozenset[type[exp.Func]] = frozenset(
    {
        # Conditions and type conversion
        exp.And,
        exp.Or,
        exp.Case,
        exp.Cast,
        exp.Coalesce,
        exp.DecodeCase,
        exp.EqualNull,
        exp.Exists,
        exp.Greatest,
        exp.If,
        exp.Least,
        exp.Nullif,
        exp.Nvl2,
        exp.TryCast,
        # Numeric
        exp.Abs,
        exp.Acos,
        exp.Asin,
        exp.Atan,
        exp.Atan2,
        exp.Ceil,
        exp.Cos,
        exp.Degrees,
        exp.Exp,
        exp.Floor,
        exp.Ln,
        exp.Log,
        exp.Pi,
        exp.Pow,
        exp.Radians,
        exp.Round,
        exp.Sign,
        exp.Sin,
        exp.Sqrt,
        exp.Tan,
        exp.Trunc,
        exp.WidthBucket,
        # Date and time
        exp.AddMonths,
        exp.ConvertTimezone,
        exp.CurrentDate,
        exp.CurrentTime,
        exp.CurrentTimestamp,
        exp.DateAdd,
        exp.DateDiff,
        exp.DateFromParts,
        exp.DateTrunc,
        exp.Day,
        exp.DayOfWeek,
        exp.DayOfWeekIso,
        exp.DayOfYear,
        exp.Extract,
        exp.Hour,
        exp.LastDay,
        exp.Localtime,
        exp.Localtimestamp,
        exp.Minute,
        exp.Month,
        exp.Quarter,
        exp.Second,
        exp.StrToTime,
        exp.TimeFromParts,
        exp.TimeSlice,
        exp.TimestampFromParts,
        exp.TimestampTrunc,
        exp.TsOrDsToDate,
        exp.TsOrDsToTime,
        exp.UnixToTime,
        exp.Week,
        exp.WeekOfYear,  # Snowflake WEEKISO
        exp.Year,
        exp.YearOfWeek,
        exp.YearOfWeekIso,
        # Text
        exp.Ascii,
        exp.ByteLength,
        exp.Chr,
        exp.Concat,
        exp.ConcatWs,
        exp.Contains,
        exp.EndsWith,
        exp.HexDecodeString,
        exp.Initcap,
        exp.Left,
        exp.Length,
        exp.Lower,
        exp.Pad,
        exp.RegexpCount,
        exp.RegexpExtract,
        exp.RegexpInstr,
        exp.RegexpLike,
        exp.RegexpReplace,
        exp.Repeat,
        exp.Replace,
        exp.Reverse,
        exp.Right,
        exp.Split,
        exp.SplitPart,
        exp.StartsWith,
        exp.StrPosition,
        exp.Substring,
        exp.ToBinary,
        exp.ToBoolean,
        exp.ToChar,
        exp.ToDouble,
        exp.ToNumber,
        exp.Translate,
        exp.Trim,
        exp.Upper,
        # Aggregates and windows
        exp.Avg,
        exp.Count,
        exp.CountIf,
        exp.CumeDist,
        exp.DenseRank,
        exp.LogicalAnd,
        exp.LogicalOr,
        exp.Max,
        exp.Median,
        exp.Min,
        exp.PercentRank,
        exp.Rank,
        exp.Stddev,
        exp.StddevPop,
        exp.StddevSamp,
        exp.Sum,
        exp.Variance,
        exp.VariancePop,
        # Semi-structured values, hashing and identifiers
        exp.Array,
        exp.ArrayAppend,
        exp.ArrayCompact,
        exp.ArrayConcat,
        exp.ArrayContains,
        exp.ArrayDistinct,
        exp.ArraySize,
        exp.GetExtract,
        exp.IsNullValue,
        exp.JSONExtract,
        exp.JSONFormat,
        exp.JSONObject,
        exp.MD5,
        exp.ObjectAgg,
        exp.ObjectInsert,
        exp.ParseJSON,
        exp.SHA,
        exp.SHA2,
        exp.Struct,
        exp.ToVariant,
        exp.Typeof,
        exp.Uuid,
    }
)
# Some native functions are represented by their SQL names rather than a typed
# operation. Keep quoted or schema-qualified routine names outside this set.
ALLOWED_UNTYPED_FUNCTIONS = frozenset(
    {
        "HASH",
        "NULLIFZERO",
        "OBJECT_DELETE",
        "TO_TIMESTAMP",
        "TO_TIMESTAMP_LTZ",
        "TO_TIMESTAMP_NTZ",
        "TO_TIMESTAMP_TZ",
        "TRY_TO_TIMESTAMP",
        "TRY_TO_TIMESTAMP_LTZ",
        "TRY_TO_TIMESTAMP_NTZ",
        "TRY_TO_TIMESTAMP_TZ",
        "ZEROIFNULL",
    }
)

_FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = (
    exp.Command,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Use,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Lateral,
    exp.TableFromRows,
    exp.MultitableInserts,
    exp.Parameter,
    exp.SessionParameter,
    exp.Placeholder,
)
_DML_TYPES: dict[type[exp.Expr], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Merge: "MERGE",
}

# A clone and its source can scan equal sort keys in different physical orders.
# Without provably unique ordering, limits, positional windows, tied extrema and
# order-sensitive aggregates can choose different business keys on identical data.
_NONREPEATABLE_OPERATIONS = (
    exp.TableSample,
    exp.Rand,
    exp.Limit,
    exp.Offset,
    exp.Fetch,
    exp.MatchRecognize,
    exp.AnyValue,
    exp.ArgMin,
    exp.ArgMax,
    exp.ArrayAgg,
    exp.GroupConcat,
    exp.FirstValue,
    exp.LastValue,
    exp.NthValue,
    exp.Lag,
    exp.Lead,
    exp.Ntile,
    exp.RowNumber,
)
_CLOCK_FUNCTIONS = (
    exp.CurrentDate,
    exp.CurrentTime,
    exp.CurrentTimestamp,
    exp.Localtime,
    exp.Localtimestamp,
)


class GuardError(ValueError):
    """Raised when a statement is not an acceptable change-request DML."""


@dataclass(frozen=True, order=True)
class TableRef:
    """A resolved three-part table name (identifier text exactly as Snowflake stores it)."""

    database: str
    schema: str
    name: str

    @classmethod
    def from_config(cls, database: str, schema: str, name: str) -> TableRef:
        """Build a reference from unquoted configuration identifiers."""
        return cls(
            normalize_identifier(database),
            normalize_identifier(schema),
            normalize_identifier(name),
        )

    @classmethod
    def parse(cls, dotted: str) -> TableRef:
        """Parse an unquoted ``DB.SCHEMA.TABLE`` configuration string."""
        parts = dotted.split(".")
        if len(parts) != 3:
            raise GuardError(f"Expected DB.SCHEMA.TABLE, got {dotted!r}")
        return cls.from_config(*parts)

    @property
    def sql(self) -> str:
        """Fully quoted SQL form."""
        return fqn(self.database, self.schema, self.name)

    def __str__(self) -> str:
        """Unquoted dotted form for messages."""
        return f"{self.database}.{self.schema}.{self.name}"


@dataclass(frozen=True)
class GuardedStatement:
    """A validated DML statement that can be rendered against any table."""

    kind: str
    target: TableRef
    reads: frozenset[TableRef]
    canonical_sql: str
    _expression: exp.Expr = field(repr=False, compare=False)

    def render_for(self, replacement: TableRef, *, lookup_snapshot_ns: int | None = None) -> str:
        """Replace the target and optionally read each physical lookup at the clone's instant."""
        if lookup_snapshot_ns is not None and (
            type(lookup_snapshot_ns) is not int or lookup_snapshot_ns <= 0
        ):
            raise ValueError("lookup_snapshot_ns must be a positive integer")
        tree = self._expression.copy()
        for table in tree.find_all(exp.Table):
            # Only physical tables were qualified by the guard. An unqualified
            # relation here is a CTE and can have the same name as the target.
            if not table.args.get("catalog"):
                continue
            ref = _resolve(table, self.target)
            if ref == self.target:
                _set_table(table, replacement)
            elif lookup_snapshot_ns is not None and ref in self.reads:
                table.set(
                    "when",
                    exp.HistoricalData(
                        this="AT",
                        kind="TIMESTAMP",
                        expression=exp.Anonymous(
                            this="TO_TIMESTAMP_LTZ",
                            expressions=[
                                exp.Literal.number(lookup_snapshot_ns),
                                exp.Literal.number(9),
                            ],
                        ),
                    ),
                )
        # Canonical SQL fully qualifies columns that name the target table.
        for column in tree.find_all(exp.Column):
            if _column_qualifier(column) == self.target:
                _set_column_qualifier(column, replacement)
        return _render_sql(tree)


def _ident_text(node: exp.Expr | None) -> str:
    if not isinstance(node, exp.Identifier):
        return ""
    return str(node.this) if node.quoted else str(node.this).upper()


def _resolve(table: exp.Table, default: TableRef) -> TableRef:
    if table.args.get("catalog") and not _ident_text(table.args.get("db")):
        raise GuardError("Specify the schema explicitly in database-qualified table names.")
    return TableRef(
        _ident_text(table.args.get("catalog")) or default.database,
        _ident_text(table.args.get("db")) or default.schema,
        _ident_text(table.this),
    )


def _set_table(table: exp.Table, ref: TableRef) -> None:
    table.set("catalog", exp.to_identifier(ref.database, quoted=True))
    table.set("db", exp.to_identifier(ref.schema, quoted=True))
    table.set("this", exp.to_identifier(ref.name, quoted=True))


def _column_qualifier(column: exp.Column) -> TableRef | None:
    """Return the fully qualified table a column names, if it is fully qualified."""
    parts = [_ident_text(column.args.get(key)) for key in ("catalog", "db", "table")]
    return TableRef(*parts) if all(parts) else None


def _set_column_qualifier(column: exp.Column, ref: TableRef) -> None:
    column.set("catalog", exp.to_identifier(ref.database, quoted=True))
    column.set("db", exp.to_identifier(ref.schema, quoted=True))
    column.set("table", exp.to_identifier(ref.name, quoted=True))


def _source_bindings(
    tree: exp.Expr, target: TableRef
) -> dict[tuple[int, tuple[str, ...]], list[TableRef | None]]:
    """Map names visible in each query block to physical tables or local aliases."""
    bindings: dict[tuple[int, tuple[str, ...]], list[TableRef | None]] = {}
    for node in tree.walk():
        alias = node.args.get("alias")
        if isinstance(alias, exp.TableAlias) and not isinstance(node, exp.CTE):
            name, ref = _ident_text(alias.this), None
        elif isinstance(node, exp.Table):
            name = _ident_text(node.this)
            ref = _resolve(node, target) if node.args.get("catalog") else None
        else:
            continue
        scope = node.find_ancestor(exp.Select, exp.DML, exp.SetOperation)
        if scope is not None:
            qualifiers = (
                [(name,)]
                if ref is None
                else [(name,), (ref.schema, name), (ref.database, ref.schema, name)]
            )
            for qualifier in qualifiers:
                bindings.setdefault((id(scope), qualifier), []).append(ref)
    return bindings


def _qualify_columns(tree: exp.Expr, target: TableRef) -> None:
    """Qualify physical table columns, preserving local aliases and CTE references."""
    bindings = _source_bindings(tree, target)
    for column in tree.find_all(exp.Column):
        if not column.table:
            continue
        qualifier = tuple(
            _ident_text(column.args[key])
            for key in ("catalog", "db", "table")
            if column.args.get(key)
        )
        scope = column.parent
        while scope is not None:
            matches = bindings.get((id(scope), qualifier))
            if matches:
                if len(matches) > 1:
                    raise GuardError(
                        f"Ambiguous column qualifier {'.'.join(qualifier)}. Use table aliases."
                    )
                if matches[0] is not None:
                    _set_column_qualifier(column, matches[0])
                break
            scope = scope.parent


def _refers_to_cte(table: exp.Table) -> bool:
    """Whether an unqualified ``table`` names a CTE that is in scope where it appears.

    Walks up to each enclosing ``WITH``. Inside the ``WITH`` itself only CTEs
    defined earlier are visible (recursive CTEs are not supported), so a name that
    shadows a real table elsewhere in the statement cannot hide that table.
    """
    if table.args.get("db") or table.args.get("catalog"):
        return False
    name = _ident_text(table.this)
    child: exp.Expr = table
    node = table.parent
    while node is not None:
        if isinstance(node, exp.With):
            for cte in node.expressions:
                cte_name = _ident_text(cte.args["alias"].this)
                if cte is child:
                    if cte_name == name:
                        raise GuardError(
                            "Recursive CTEs are not supported. Qualify a physical table "
                            "that has the same name as its CTE."
                        )
                    break  # CTEs from here on are not yet defined
                if cte_name == name:
                    return True
        else:
            with_ = node.args.get("with_") if node.args else None
            if (
                isinstance(with_, exp.With)
                and child is not with_
                and name in {_ident_text(c.args["alias"].this) for c in with_.expressions}
            ):
                return True
        child, node = node, node.parent
    return False


def _parse_single(sql: str) -> exp.Expr:
    if not sql.strip():
        raise GuardError("The statement is empty.")
    if len(sql) > MAX_SQL_LENGTH:
        raise GuardError(f"The statement exceeds {MAX_SQL_LENGTH} characters.")
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (SqlglotError, RecursionError) as error:
        raise GuardError(f"Could not parse the statement: {error}") from error
    if len(statements) != 1:
        raise GuardError(f"Exactly one statement is allowed, found {len(statements)}.")
    return statements[0]


def _render_sql(tree: exp.Expr) -> str:
    try:
        return tree.sql(dialect=DIALECT, unsupported_level=ErrorLevel.RAISE)
    except (SqlglotError, RecursionError) as error:
        raise GuardError(f"Could not render the Snowflake statement: {error}") from error


def _is_sequence_reference(node: exp.Expr) -> bool:
    return isinstance(node, exp.NextValueFor) or (
        isinstance(node, exp.Column) and bool(node.table) and _ident_text(node.this) == "NEXTVAL"
    )


def has_sequence_default(expression: str | None) -> bool:
    """Detect defaults that could advance an external sequence on a table clone."""
    if not expression or not expression.strip():
        return False
    # Identity columns use Snowflake's own identity generator. Some metadata
    # surfaces describe them using their column DDL instead of a scalar default.
    if re.fullmatch(
        r"(?:AUTOINCREMENT|IDENTITY)(?:\s*\(\s*-?\d+\s*,\s*-?\d+\s*\)|"
        r"\s+START\s+-?\d+\s+INCREMENT\s+-?\d+)?(?:\s+(?:NOORDER|ORDER))?",
        expression.strip(),
        re.IGNORECASE,
    ):
        return False
    try:
        tree = _parse_single(expression)
    except GuardError:
        return True
    return isinstance(tree, (exp.Command, exp.DDL, exp.DML)) or any(
        _is_sequence_reference(node) for node in tree.walk()
    )


def _in_write_value(node: exp.Expr) -> bool:
    """Whether this expression supplies a scalar SET assignment or explicit INSERT value."""
    child = node
    while (parent := child.parent) is not None:
        # A projected clock can later affect WHERE, JOIN, HAVING, QUALIFY or a
        # correlated subquery through an alias. Do not infer dataflow across queries.
        if isinstance(parent, (exp.Query, exp.Window)):
            return False
        if isinstance(parent, exp.Insert):
            return child is parent.expression and isinstance(child, (exp.Values, exp.Tuple))
        if (
            isinstance(parent, exp.EQ)
            and child is parent.expression
            and isinstance(parent.parent, exp.Update)
            and parent.arg_key == "expressions"
        ):
            return True
        child = parent
    return False


def _check_repeatable_rows(node: exp.Expr) -> None:
    if (
        isinstance(node, _NONREPEATABLE_OPERATIONS)
        or (isinstance(node, exp.Distinct) and node.args.get("on"))
        or (isinstance(node, exp.WindowSpec) and node.text("kind").upper() == "ROWS")
        or (isinstance(node, exp.Join) and node.method == "ASOF")
        or (isinstance(node, exp.Uuid) and not (node.this and node.args.get("name")))
    ):
        raise GuardError(
            f"{type(node).__name__} cannot guarantee repeatable results for the approved change. "
            "Use an explicit key or a deterministic aggregate."
        )
    if isinstance(node, _CLOCK_FUNCTIONS) and not _in_write_value(node):
        raise GuardError(
            "Use a fixed date or time to select rows. Clock values require a write assignment."
        )


def _check_forbidden(tree: exp.Expr) -> None:
    for node in tree.walk():
        _check_repeatable_rows(node)
        if isinstance(node, _FORBIDDEN_NODES) or (
            isinstance(node, exp.UDTF) and not isinstance(node, exp.Values)
        ):
            raise GuardError(f"{type(node).__name__} is not allowed in a change request.")
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            name = node.expression.name or node.expression.sql_name()
            raise GuardError(f"Qualified function {name.upper()}() is not allowed.")
        if _is_sequence_reference(node):
            raise GuardError("Sequence NEXTVAL references are not allowed in a sandbox.")
        if isinstance(node, exp.Func) and not _allowed_function(node):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            raise GuardError(
                f"Function {name}() is not an allowed built-in "
                "(user-defined and system functions are blocked)."
            )
        # MERGE actions are represented by nested DML nodes in SQLGlot.
        if (
            isinstance(node, exp.DML)
            and node is not tree
            and not (
                isinstance(tree, exp.Merge)
                and isinstance(node.parent, exp.When)
                and node.arg_key == "then"
            )
        ):
            raise GuardError("Only the top-level change may write to a table.")


def _allowed_function(node: exp.Func) -> bool:
    if isinstance(node, exp.Anonymous):
        return node.name.upper() in ALLOWED_UNTYPED_FUNCTIONS and not (
            isinstance(node.this, exp.Identifier) and node.this.quoted
        )
    return type(node) in _ALLOWED_FUNCTION_NODES


def _preserve_epoch_types(tree: exp.Expr) -> None:
    """Keep explicit epoch overloads that SQLGlot's Snowflake generator emits as TO_TIMESTAMP."""
    names = {
        exp.DType.TIMESTAMPLTZ: "TO_TIMESTAMP_LTZ",
        exp.DType.TIMESTAMPNTZ: "TO_TIMESTAMP_NTZ",
        exp.DType.TIMESTAMPTZ: "TO_TIMESTAMP_TZ",
    }
    for conversion in list(tree.find_all(exp.UnixToTime)):
        target = conversion.args.get("target_type")
        if isinstance(target, exp.DataType) and target.this in names:
            args = [conversion.this]
            if scale := conversion.args.get("scale"):
                args.append(scale)
            conversion.replace(exp.Anonymous(this=names[target.this], expressions=args))


def _written_table(tree: exp.Expr) -> exp.Table:
    written = tree.this
    if isinstance(written, exp.Schema):  # INSERT INTO t (col, ...)
        written = written.this
    if not isinstance(written, exp.Table) or not isinstance(written.this, exp.Identifier):
        raise GuardError("Could not determine the table being modified.")
    return written


def guard_dml(
    sql: str, target: TableRef, allowed_reads: frozenset[TableRef] = frozenset()
) -> GuardedStatement:
    """Validate ``sql`` as a change to ``target`` and return its canonical form.

    Unqualified table names resolve against the target's database and schema.

    Raises:
        GuardError: if any rule in the module docstring is violated.

    """
    tree = _parse_single(sql)
    kind = _DML_TYPES.get(type(tree))
    if kind is None:
        raise GuardError("Only INSERT, UPDATE, DELETE and MERGE statements are allowed.")
    if tree.args.get("with_"):
        raise GuardError("Snowflake requires WITH inside the SELECT source of a DML statement.")
    if isinstance(tree, exp.Insert) and tree.args.get("overwrite"):
        raise GuardError("INSERT OVERWRITE is not allowed.")
    write_target = _written_table(tree)
    written = _resolve(write_target, target)
    if written != target:
        raise GuardError(f"The statement modifies {written}, but this request is for {target}.")

    reads: set[TableRef] = set()
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier):
            kind_name = type(table.this).__name__
            raise GuardError(
                f"Table functions ({kind_name}) and computed table references are not allowed."
            )
        if any(table.args.get(key) for key in ("when", "changes", "version")):
            raise GuardError("Time Travel and CHANGES are not allowed on change-request tables.")
        if table is not write_target and _refers_to_cte(table):
            continue
        ref = _resolve(table, target)
        if ref != target and ref not in allowed_reads:
            raise GuardError(
                f"Reading {ref} is not allowed for this table. "
                "Ask an admin to add it to allowed_read_tables."
            )
        _set_table(table, ref)
        if ref != target:
            reads.add(ref)
    _check_forbidden(tree)
    _qualify_columns(tree, target)
    _preserve_epoch_types(tree)

    return GuardedStatement(
        kind=kind,
        target=target,
        reads=frozenset(reads),
        canonical_sql=_render_sql(tree),
        _expression=tree,
    )

# ==================================================================
# auditable_crud/state.py
# ==================================================================
"""Change-request lifecycle and separation-of-duties rules.

``DRAFT -> DRY_RUN_OK -> SUBMITTED -> APPROVED -> EXECUTING -> EXECUTED | FAILED``

* A dry run may be repeated (``DRAFT|DRY_RUN_OK -> DRY_RUN_OK``).
* An admin may reject a submitted request, or send an approved one back to
  ``SUBMITTED`` when the source changed after approval.
* The requester may cancel any request that has not started executing.
"""


from enum import StrEnum


class Status(StrEnum):
    """States of a change request (stored verbatim in CHANGE_REQUESTS.STATUS)."""

    DRAFT = "DRAFT"
    DRY_RUN_OK = "DRY_RUN_OK"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DRAFT: frozenset({Status.DRY_RUN_OK, Status.CANCELLED}),
    Status.DRY_RUN_OK: frozenset({Status.DRY_RUN_OK, Status.SUBMITTED, Status.CANCELLED}),
    Status.SUBMITTED: frozenset({Status.APPROVED, Status.REJECTED, Status.CANCELLED}),
    # After a rolled-back execution the durable state is still APPROVED. Record
    # the failed attempt in its own transaction guarded by the approval revision.
    Status.APPROVED: frozenset(
        {Status.EXECUTING, Status.FAILED, Status.SUBMITTED, Status.CANCELLED}
    ),
    Status.EXECUTING: frozenset({Status.EXECUTED, Status.FAILED}),
    Status.EXECUTED: frozenset(),
    Status.FAILED: frozenset(),
    Status.REJECTED: frozenset(),
    Status.CANCELLED: frozenset(),
}

TERMINAL_STATUSES = frozenset(s for s, targets in TRANSITIONS.items() if not targets)
EDITABLE_STATUSES = frozenset({Status.DRAFT, Status.DRY_RUN_OK})


class WorkflowError(ValueError):
    """Raised when an action is not permitted in the request's current state."""


def ensure_transition(current: str, new: Status) -> Status:
    """Validate ``current -> new`` and return the parsed current status."""
    try:
        status = Status(current)
    except ValueError as error:
        raise WorkflowError(f"Unknown status {current!r}") from error
    if new not in TRANSITIONS[status]:
        raise WorkflowError(f"A request in status {status} cannot move to {new}.")
    return status


def same_user(a: str, b: str) -> bool:
    """Compare Snowflake user names (case-insensitive, like unquoted identifiers)."""
    return a.strip().upper() == b.strip().upper()


def ensure_not_requester(requested_by: str, actor: str, action: str) -> None:
    """Enforce separation of duties: requesters never review or run their own change."""
    if same_user(requested_by, actor):
        raise WorkflowError(f"{actor} requested this change and cannot {action} it.")


def ensure_requester(requested_by: str, actor: str, action: str) -> None:
    """Only the requester may edit, dry-run, submit or cancel their request."""
    if not same_user(requested_by, actor):
        raise WorkflowError(f"Only {requested_by} can {action} this request.")

# ==================================================================
# auditable_crud/diff.py
# ==================================================================
"""Turning a stream on the clone into a precise, browsable change set.

The stream is created on the clone *before* the DML runs, so afterwards it
holds exactly the rows the statement touched. Its contents are copied into a
"diff" table (``METADATA$`` columns renamed to ``AC_*``) so that the result is
stable and can be browsed by developer and admin long after the dry run.

An UPDATE shows up as a DELETE/INSERT pair sharing ``METADATA$ROW_ID`` with
``METADATA$ISUPDATE = TRUE``: the DELETE side is the row before, the INSERT
side the row after.
"""


from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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

# ==================================================================
# auditable_crud/drift.py
# ==================================================================
"""Detecting what happened to the source table since a change was dry-run.

The dry run captured ``snapshot`` (epoch nanoseconds) and cloned the source
*at* that instant. ``CHANGES(...) AT(TIMESTAMP => snapshot)`` on the source then
returns exactly the net changes committed by anyone else since then. We report
them and, using the table's key columns, how many of those rows are also rows
the change request touches ("overlap").
"""


from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any



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

# ==================================================================
# auditable_crud/config.py
# ==================================================================
"""Managed-table configuration, read from the DCM-defined ``CORE.V_MANAGED_TABLES`` view."""


from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


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

# ==================================================================
# auditable_crud/runner.py
# ==================================================================
"""Thin SQL execution boundary shared by the procedure and the apps.

Business logic depends only on :class:`SqlRunner`, so it can be unit-tested
with an in-memory fake and run unchanged on a Snowpark session.
"""


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

# ==================================================================
# auditable_crud/store.py
# ==================================================================
"""Persistence for change requests and the audit log (``CORE`` schema tables)."""


import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol


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

# ==================================================================
# auditable_crud/engine.py
# ==================================================================
"""Change-request workflow, shared by the two owner's-rights procedures.

Every state change re-validates its preconditions server-side: the apps are
convenience UIs, this module is the authority. Status changes use
compare-and-set (``UPDATE ... WHERE STATUS = <expected>``) so double clicks or
two admins acting at once cannot both win.
"""


import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


RESERVED_COLUMNS = frozenset({"AC_ACTION", "AC_ISUPDATE", "AC_ROW_ID"})
MAX_TEXT = 10_000


def sha256(text: str) -> str:
    """Hex SHA-256 of ``text`` (used to pin the approved statement)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def within_tolerance(expected: DiffCounts, actual: DiffCounts, tolerance_pct: float) -> bool:
    """Return True when inserted/updated/deleted are each within ``tolerance_pct``."""
    for exp_value, act_value in (
        (expected.inserted, actual.inserted),
        (expected.updated, actual.updated),
        (expected.deleted, actual.deleted),
    ):
        if 100 * abs(act_value - exp_value) > exp_value * tolerance_pct:
            return False
    return True


def _clean_text(value: Any, field: str, *, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise WorkflowError(f"{field} is required.")
    if len(text) > MAX_TEXT:
        raise WorkflowError(f"{field} is longer than {MAX_TEXT} characters.")
    return text


def _sandbox_label(request_id: str, actor: str, source: TableRef, purpose: str) -> str:
    return f"Auditable CRUD {purpose} for request {request_id} by {actor} on {source}"


def _number(args: Mapping[str, Any], key: str, default: float) -> float:
    """Numeric argument with a default; unlike ``x or default``, keeps an explicit 0."""
    value = args.get(key)
    return default if value is None or value == "" else float(value)


def _days(args: Mapping[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    if type(value) is int or (isinstance(value, str) and value.isdecimal()):
        days = int(value)
        if days >= 0:
            return days
    raise WorkflowError(f"{key} must be a non-negative whole number of days.")


def _counts(value: Any) -> DiffCounts:
    return DiffCounts.from_row({k.upper(): v for k, v in (value or {}).items()})


class _ExecutionCountError(WorkflowError):
    """Snowflake reported ambiguous update matches or counts outside the approval."""


class _RevalidationRequiredError(WorkflowError):
    """The recorded sandbox no longer provides an approval baseline."""

    def __init__(self, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(f"{reason} Re-validate on current data before approving.")
        self.reason = reason
        self.details = details


class _DefinitionChangedError(_RevalidationRequiredError):
    """A table definition can no longer be verified against the reviewed run."""


@dataclass
class _DmlAttempt:
    """Evidence obtained from the source statement, including a rolled-back attempt."""

    query_id: str | None = None
    elapsed_ms: int | None = None
    counts: DiffCounts | None = None

    def details(self) -> Row:
        values: Row = {}
        if self.query_id:
            values["query_id"] = self.query_id
        if self.elapsed_ms is not None:
            values["elapsed_ms"] = self.elapsed_ms
        if self.counts is not None:
            values["counts"] = self.counts.as_dict()
        return values


class Engine:
    """Implements every change-request operation."""

    def __init__(
        self,
        runner: DmlRunner,
        store: RequestStore,
        *,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """Create an engine; ``new_id`` and ``clock`` are injectable for tests."""
        self._sql = runner
        self._store = store
        self._new_id = new_id
        self._clock = clock
        self._rollback_uncertain = False

    def _rollback(self, error: Exception) -> None:
        try:
            self._store.rollback()
        except Exception as rollback_error:
            # More SQL on this session, especially DDL, could commit the failed operation.
            self._rollback_uncertain = True
            raise error from rollback_error

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        self._store.begin()
        try:
            yield
            self._store.commit()
        except Exception as error:
            self._rollback(error)
            raise

    # ------------------------------------------------------------------ helpers
    def _config(self, table_id: str) -> TableConfig:
        return TableConfig.from_row(self._store.load_config(table_id))

    def _request_config(self, request: Mapping[str, Any]) -> TableConfig:
        cfg = self._config(str(request["TABLE_ID"]))
        if str(cfg.source) != request["SOURCE_FQN"]:
            raise WorkflowError(
                "The managed table now points to a different source. Open a new request."
            )
        return cfg

    def _now_ns(self) -> int:
        value = scalar(self._sql, "SELECT DATE_PART(EPOCH_NANOSECOND, CURRENT_TIMESTAMP()) AS NS")
        return int(value)

    def _column_metadata(self, table: TableRef) -> list[Row]:
        return self._sql.query(
            "SELECT COLUMN_NAME, COLUMN_DEFAULT, DATA_TYPE, IS_NULLABLE, "
            "CHARACTER_MAXIMUM_LENGTH, CHARACTER_OCTET_LENGTH, NUMERIC_PRECISION, "
            "NUMERIC_SCALE, DATETIME_PRECISION, IS_IDENTITY, IDENTITY_GENERATION, "
            "IDENTITY_START, IDENTITY_INCREMENT "
            f"FROM {information_schema(table.database)}.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            [table.schema, table.name],
        )

    def _source_columns(self, cfg: TableConfig, table: TableRef | None = None) -> list[str]:
        rows = self._column_metadata(table or cfg.source)
        columns = [str(r["COLUMN_NAME"]) for r in rows]
        if not columns:
            raise WorkflowError(f"Source table {cfg.source} was not found or is not readable.")
        if any(has_sequence_default(r.get("COLUMN_DEFAULT")) for r in rows):
            raise WorkflowError(f"{cfg.source} has a sequence default shared by its clones.")
        if RESERVED_COLUMNS & set(columns):
            raise WorkflowError(f"Source columns may not be named {sorted(RESERVED_COLUMNS)}.")
        missing = set(cfg.key_columns) - set(columns)
        if missing:
            raise WorkflowError(f"Key columns {sorted(missing)} do not exist in {cfg.source}.")
        return columns

    def _check_tracking(self, ref: TableRef, snapshot_ns: int) -> None:
        retention = scalar(
            self._sql,
            f"SELECT RETENTION_TIME FROM {information_schema(ref.database)}.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [ref.schema, ref.name],
        )
        if not retention or int(retention) < 1:
            raise WorkflowError(f"{ref} needs at least one day of Time Travel retention.")
        # An empty, explicitly bounded interval checks the same CHANGES capability
        # that approval will need, before any sandbox DDL or user DML.
        try:
            self._sql.query(drift_counts_sql(ref.sql, snapshot_ns, snapshot_ns))
        except Exception as error:
            raise WorkflowError(
                f"Check change tracking and read access on {ref}: {error}"
            ) from error

    def _build_sandbox(
        self, cfg: TableConfig, clone: TableRef, stream: TableRef, label: str, snapshot_ns: int
    ) -> None:
        """Create a clone at the specified snapshot and a stream on it."""
        self._sql.query(
            f"CREATE OR REPLACE TABLE {clone.sql} CLONE {cfg.source.sql} "
            f"AT(TIMESTAMP => TO_TIMESTAMP_LTZ({snapshot_ns}, 9))"
        )
        # Clones inherit the source's comment; replace it so the object explains itself.
        self._sql.query(f"ALTER TABLE {clone.sql} SET COMMENT = {sql_string_literal(label)}")
        # The stream must exist before the DML so it captures exactly its effect.
        self._sql.query(f"CREATE OR REPLACE STREAM {stream.sql} ON TABLE {clone.sql}")

    def _run_dml(self, sql: str, attempt: _DmlAttempt | None = None) -> tuple[DiffCounts, str, int]:
        evidence = attempt if attempt is not None else _DmlAttempt()
        started = self._clock()
        try:
            rows = self._sql.query_dml(sql)
        except Exception as error:
            # A failed Snowpark statement carries its own ID, even if no counts were returned.
            evidence.query_id = getattr(error, "sfqid", None)
            raise
        finally:
            evidence.elapsed_ms = round((self._clock() - started) * 1000)
        query_id = str(scalar(self._sql, "SELECT LAST_QUERY_ID() AS QID") or "")
        evidence.query_id = query_id
        row = {label.lower(): value for label, value in rows[0].items()} if rows else {}
        evidence.counts = DiffCounts.from_dml_result(row)
        multi_joined = int(row.get("number of multi-joined rows updated", 0))
        if multi_joined > 0:
            raise _ExecutionCountError(
                f"UPDATE {query_id} matched multiple source rows for {multi_joined} target "
                "row(s). Make the join return a single source row per target row."
            )
        return evidence.counts, query_id, evidence.elapsed_ms

    def _sandboxed_run(
        self,
        cfg: TableConfig,
        guarded: GuardedStatement,
        names: tuple[TableRef, ...],
        label: str,
    ) -> dict[str, Any]:
        """Clone, stream, run the DML on the clone, materialize and measure its diff."""
        clone, stream, diff = names
        snapshot_ns = self._now_ns()
        for ref in sorted({cfg.source, *guarded.reads}, key=str):
            self._check_tracking(ref, snapshot_ns)
        self._build_sandbox(cfg, clone, stream, label, snapshot_ns)
        # Defaults belong to the cloned schema as of the snapshot. Even an INSERT
        # omitting a sequence-backed column could advance the original sequence.
        columns = self._source_columns(cfg, clone)
        counts, query_id, elapsed_ms = self._run_dml(
            guarded.render_for(clone, lookup_snapshot_ns=snapshot_ns)
        )
        self._sql.query(materialize_sql(diff.sql, stream.sql))
        diff_counts = DiffCounts.from_row(self._sql.query(counts_sql(diff.sql))[0])
        fingerprint = scalar(self._sql, fingerprint_sql(diff.sql, columns))
        return {
            "snapshot_ns": snapshot_ns,
            "counts": counts,
            "diff_counts": diff_counts,
            "fingerprint": None if fingerprint is None else str(fingerprint),
            "query_id": query_id,
            "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _names(request: Mapping[str, Any]) -> SandboxNames:
        return SandboxNames(base=str(request["SANDBOX_BASE"]))

    @staticmethod
    def _ref(request: Mapping[str, Any], name: str) -> TableRef:
        return TableRef(str(request["CLONE_DATABASE"]), str(request["CLONE_SCHEMA"]), name)

    def _transition(
        self,
        request: Mapping[str, Any],
        new: Status,
        values: Mapping[str, Any],
        expected_revision: int | None = None,
    ) -> None:
        current = ensure_transition(str(request["STATUS"]), new)
        updated = self._store.update(
            str(request["REQUEST_ID"]),
            {**values, "STATUS": str(new)},
            expected_status=current,
            expected_revision=expected_revision,
        )
        if updated != 1:
            raise WorkflowError("The request was changed by someone else; reload and retry.")

    def _record(
        self,
        request: Mapping[str, Any],
        new: Status | None,
        values: Mapping[str, Any],
        event: str,
        actor: str,
        *,
        details: Mapping[str, Any],
        expected_revision: int | None = None,
    ) -> None:
        """Apply a change and its audit event in one transaction: both or neither."""
        request_id = str(request["REQUEST_ID"])
        with self._transaction():
            if new is None:
                if (
                    self._store.update(
                        request_id,
                        values,
                        expected_status=str(request["STATUS"]),
                        expected_revision=expected_revision,
                    )
                    != 1
                ):
                    raise WorkflowError(
                        "The request was changed by someone else; reload and retry."
                    )
            else:
                self._transition(request, new, values, expected_revision)
            self._store.audit(request_id, event, actor, details)

    @staticmethod
    def _revision(request: Mapping[str, Any]) -> int:
        return int(request.get("REVISION") or 0)

    def _claim_run(self, request: Mapping[str, Any]) -> int:
        """Start a sandbox run: bump REVISION so any earlier or concurrent result is stale.

        Returns the new revision; the run's results are only stored if it is still
        current, and submit / approval require the latest *completed* run
        (``RUN_REVISION``) to be the current revision.
        """
        revision = self._revision(request) + 1
        with self._transaction():
            claimed = self._store.update(
                str(request["REQUEST_ID"]),
                {"REVISION": revision, "LAST_ERROR": None},
                expected_status=str(request["STATUS"]),
                expected_revision=revision - 1,
            )
            if claimed != 1:
                raise WorkflowError("The request was changed by someone else; reload and retry.")
        return revision

    @contextlib.contextmanager
    def _attempt(
        self,
        request: Mapping[str, Any],
        actor: str,
        revision_field: str,
        failure_event: str,
    ) -> Iterator[tuple[int, tuple[TableRef, ...]]]:
        revision = self._claim_run(request)
        names = self._names(request).for_revision(revision)
        refs = tuple(self._ref(request, n) for n in (names.clone, names.stream, names.diff))
        try:
            yield revision, refs
        except Exception as error:
            if self._rollback_uncertain:
                raise
            # A failed or overtaken run owns only its own provisional objects.
            # Keep the published result if a COMMIT acknowledgement was lost.
            with contextlib.suppress(Exception):
                if self._store.get(str(request["REQUEST_ID"])).get(revision_field) != revision:
                    with contextlib.suppress(Exception):
                        self._drop_attempt(request, names)
                    self._record(
                        request,
                        None,
                        {"LAST_ERROR": str(error)[:MAX_TEXT]},
                        failure_event,
                        actor,
                        details={"error": str(error)},
                        expected_revision=revision,
                    )
            raise

    def _check_displayed_revision(self, request: Mapping[str, Any], displayed: int | None) -> None:
        if displayed is not None and displayed != self._revision(request):
            raise WorkflowError(
                "The request has changed since it was displayed. Reload before continuing."
            )

    def _ensure_run_current(self, request: Mapping[str, Any], what: str) -> int:
        revision = self._revision(request)
        run_revision = request.get("RUN_REVISION")
        # Requests created before revisions existed have neither column set.
        if (0 if run_revision is None else int(run_revision)) != revision:
            raise WorkflowError(
                f"A sandbox run is in progress or did not finish, so there is nothing "
                f"consistent to {what}. Wait for it, or run it again."
            )
        return revision

    def _baseline(self, request: Mapping[str, Any]) -> tuple[int, TableRef]:
        """Snapshot and diff table of the most recent sandbox run (re-validation wins)."""
        names = self._names(request)
        if request.get("REVAL_SNAPSHOT_NS"):
            return int(request["REVAL_SNAPSHOT_NS"]), self._ref(
                request, names.published(request, "reval_diff")
            )
        if not request.get("DRY_RUN_AT") or not request.get("SNAPSHOT_NS"):
            raise WorkflowError("The request has not been dry-run yet.")
        return int(request["SNAPSHOT_NS"]), self._ref(request, names.published(request, "diff"))

    def _check_definition_time(self, table: TableRef, baseline_ns: int, *, lookup: bool) -> None:
        # Time Travel reads use current column definitions. CHANGES alone cannot
        # detect a column rename or a revised expression that changes the DML.
        timestamp = scalar(
            self._sql,
            "SELECT DATE_PART(EPOCH_NANOSECOND, COALESCE(LAST_DDL, CREATED)) AS DDL_NS "
            f"FROM {information_schema(table.database)}.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [table.schema, table.name],
        )
        last_ddl_ns = int(timestamp) if timestamp is not None else None
        if last_ddl_ns is not None and last_ddl_ns <= baseline_ns:
            return
        role = "lookup" if lookup else "target"
        if last_ddl_ns is None:
            message = f"The definition timestamp of the {role} table {table} is unavailable."
            reason = f"{role.upper()}_DDL_UNAVAILABLE"
        else:
            message = (
                f"The {role} table definition for {table} changed after the reviewed snapshot."
            )
            reason = f"{role.upper()}_DDL_CHANGED"
        raise _DefinitionChangedError(
            message,
            {
                "reason": reason,
                "table_fqn": str(table),
                "baseline_ns": baseline_ns,
                "last_ddl_ns": last_ddl_ns,
            },
        )

    def _check_review_baseline(self, request: Mapping[str, Any], cfg: TableConfig) -> DriftReport:
        if request.get("RUN_CONTEXT_VERSION") != DML_CONTEXT_VERSION:
            raise _RevalidationRequiredError(
                "The last sandbox run used earlier SQL session settings.",
                {"reason": "RUN_CONTEXT_CHANGED", "required_version": DML_CONTEXT_VERSION},
            )
        # CHANGES tracks row DML. A schema-only change can alter the values an
        # approved INSERT will write without affecting its row count.
        kind = "reval_clone" if request.get("REVAL_SNAPSHOT_NS") else "clone"
        clone = self._ref(request, self._names(request).published(request, kind))
        reviewed_columns = self._column_metadata(clone)
        if not reviewed_columns or reviewed_columns != self._column_metadata(cfg.source):
            raise _DefinitionChangedError(
                f"The schema of {cfg.source} no longer matches the reviewed clone.",
                {"reason": "SCHEMA_CHANGED", "source": str(cfg.source)},
            )
        try:
            report = self._drift(request, cfg)
        except Exception as error:
            message = str(getattr(error, "raw_message", None) or error)
            code = str(getattr(error, "sql_error_code", None) or "").lstrip("0")
            if code == "707" or (
                not code and "time travel data is not available for table" in message.casefold()
            ):
                reason = "Time Travel history is unavailable at the reviewed snapshot."
                category = "HISTORY_UNAVAILABLE"
            elif code == "91930" or (
                not code
                and "change tracking is not enabled or has been missing" in message.casefold()
            ):
                reason = (
                    "Change tracking is unavailable for the reviewed interval. Ask the table "
                    "owner to verify change tracking on the target and each referenced lookup."
                )
                category = "CHANGE_TRACKING_UNAVAILABLE"
            else:
                raise
            raise _RevalidationRequiredError(
                reason,
                {"reason": category, "error": message},
            ) from error
        if report.total:
            raise _RevalidationRequiredError(
                f"The source changed since the last sandbox run ({report.total} rows, "
                f"{report.overlapping_keys} overlapping).",
                report.as_dict(),
            )
        return report

    def _drift(self, request: Mapping[str, Any], cfg: TableConfig) -> DriftReport:
        baseline_ns, diff = self._baseline(request)
        self._check_definition_time(cfg.source, baseline_ns, lookup=False)
        end_ns = self._now_ns()
        counts = self._sql.query(drift_counts_sql(cfg.source.sql, baseline_ns, end_ns))[0]
        overlap = self._sql.query(
            drift_overlap_sql(cfg.source.sql, baseline_ns, diff.sql, cfg.key_columns, end_ns)
        )[0]
        # Tables the statement reads (MERGE ... USING, INSERT ... SELECT) change its
        # effect too, so changes to them count as drift as well.
        read_changes = 0
        if request.get("CANONICAL_SQL"):
            guarded = guard_dml(str(request["CANONICAL_SQL"]), cfg.source, cfg.allowed_reads)
            for ref in sorted(guarded.reads, key=str):
                self._check_definition_time(ref, baseline_ns, lookup=True)
                row = self._sql.query(drift_counts_sql(ref.sql, baseline_ns, end_ns))[0]
                read_changes += sum(
                    int(row.get(k) or 0) for k in ("INSERTED", "UPDATED", "DELETED")
                )
        return build_report(
            baseline_ns, counts, overlap, read_changes, end_ns=end_ns, diff_fqn=str(diff)
        )

    # --------------------------------------------------------------- operations
    def create_request(self, table_id: str, actor: str) -> dict[str, Any]:
        """Open a DRAFT request and clone the source into the sandbox."""
        cfg = self._config(table_id)
        self._source_columns(cfg)
        snapshot_ns = self._now_ns()
        self._check_tracking(cfg.source, snapshot_ns)
        request_id = self._new_id()
        names = build_sandbox_names(cfg.source.name, actor, request_id, datetime.now(UTC))
        with self._transaction():
            self._store.insert(
                {
                    "REQUEST_ID": request_id,
                    "TABLE_ID": cfg.table_id,
                    "SOURCE_FQN": str(cfg.source),
                    "STATUS": str(Status.DRAFT),
                    "REQUESTED_BY": actor,
                    "CLONE_DATABASE": cfg.clone_database,
                    "CLONE_SCHEMA": cfg.clone_schema,
                    "SANDBOX_BASE": names.base,
                    "SNAPSHOT_NS": snapshot_ns,
                    "REVISION": 0,
                    "RUN_REVISION": -1,
                    "CREATED_AT": NOW,
                    "UPDATED_AT": NOW,
                }
            )
            self._store.audit(request_id, "CREATED", actor, {"clone": names.clone})
        # Register the names before doing DDL, so partial creation is discoverable.
        # All subsequent sandbox attempts have their own names and snapshot.
        try:
            self._build_sandbox(
                cfg,
                cfg.sandbox_ref(names.clone),
                cfg.sandbox_ref(names.stream),
                _sandbox_label(request_id, actor, cfg.source, "dry-run sandbox"),
                snapshot_ns,
            )
        except Exception as error:
            try:
                with contextlib.suppress(WorkflowError):
                    self._record(
                        {"REQUEST_ID": request_id, "STATUS": str(Status.DRAFT)},
                        Status.CANCELLED,
                        {"CANCELLED_AT": NOW, "LAST_ERROR": str(error)[:MAX_TEXT]},
                        "SANDBOX_CREATION_FAILED",
                        actor,
                        details={"error": str(error)},
                        expected_revision=0,
                    )
            finally:
                # A concurrent cancellation and cleanup can finish before this
                # attempt creates its table. Discard any partial DDL from this call.
                if not self._rollback_uncertain:
                    self._discard_initial_sandbox(request_id, names, actor)
            raise
        with self._transaction():
            finished = self._store.update(request_id, {"RUN_REVISION": 0}, str(Status.DRAFT), 0)
        if not finished:
            current = self._store.get(request_id)
            if current["STATUS"] == Status.CANCELLED or current.get("SANDBOX_DROPPED_AT"):
                self._discard_initial_sandbox(request_id, names, actor)
                raise WorkflowError(
                    f"Request {request_id} is now {current['STATUS']}. "
                    "Its initial sandbox is no longer available."
                )
        return {"request_id": request_id, "clone": str(cfg.sandbox_ref(names.clone))}

    def dry_run(
        self, request_id: str, actor: str, sql_text: str, nl_prompt: str | None = None
    ) -> dict[str, Any]:
        """Validate the SQL, run it on a fresh clone and record exactly what changed."""
        request = self._store.get(request_id)
        ensure_requester(str(request["REQUESTED_BY"]), actor, "dry-run")
        if Status(str(request["STATUS"])) not in EDITABLE_STATUSES:
            raise WorkflowError("Only draft requests can be dry-run; create a new request.")
        cfg = self._request_config(request)
        sql_text = _clean_text(sql_text, "SQL", required=True)
        prompt = _clean_text(nl_prompt, "Prompt") or None
        guarded = guard_dml(sql_text, cfg.source, cfg.allowed_reads)
        with self._attempt(request, actor, "DRY_SANDBOX_REVISION", "DRY_RUN_FAILED") as (
            revision,
            refs,
        ):
            run = self._sandboxed_run(
                cfg, guarded, refs, _sandbox_label(request_id, actor, cfg.source, "dry-run sandbox")
            )
            values = {
                "SQL_TEXT": sql_text,
                "CANONICAL_SQL": guarded.canonical_sql,
                "SQL_SHA256": sha256(guarded.canonical_sql),
                "STATEMENT_KIND": guarded.kind,
                "NL_PROMPT": prompt,
                "SNAPSHOT_NS": run["snapshot_ns"],
                "DRY_RUN_COUNTS": run["counts"].as_dict(),
                "DRY_RUN_DIFF_COUNTS": run["diff_counts"].as_dict(),
                "DRY_RUN_FINGERPRINT": run["fingerprint"],
                "DRY_RUN_QUERY_ID": run["query_id"],
                "DRY_RUN_ELAPSED_MS": run["elapsed_ms"],
                "DRY_RUN_AT": NOW,
                "LAST_ERROR": None,
                "RUN_CONTEXT_VERSION": DML_CONTEXT_VERSION,
                "RUN_REVISION": revision,
                "DRY_SANDBOX_REVISION": revision,
            }
            result = {
                "request_id": request_id,
                "kind": guarded.kind,
                "counts": run["counts"].as_dict(),
                "diff_counts": run["diff_counts"].as_dict(),
                "elapsed_ms": run["elapsed_ms"],
                "query_id": run["query_id"],
                "max_affected_rows": cfg.max_affected_rows,
            }
            self._record(
                request,
                Status.DRY_RUN_OK,
                values,
                "DRY_RUN_OK",
                actor,
                details=result,
                expected_revision=revision,
            )
            return result

    def submit(
        self,
        request_id: str,
        actor: str,
        note: str | None = None,
        sql: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Send a successfully dry-run request to the admins.

        ``sql`` is the statement the requester is looking at; when given it must be
        the one that was dry-run, so a later edit cannot be submitted unseen.
        """
        request = self._store.get(request_id)
        ensure_requester(str(request["REQUESTED_BY"]), actor, "submit")
        self._check_displayed_revision(request, expected_revision)
        if sql is not None and sql.strip() != str(request.get("SQL_TEXT") or "").strip():
            raise WorkflowError(
                "The SQL was edited after the last dry run. "
                "Run the dry run again before submitting."
            )
        cfg = self._request_config(request)
        counts = _counts(request.get("DRY_RUN_COUNTS"))
        if counts.total == 0:
            raise WorkflowError("The dry run affected no rows; there is nothing to submit.")
        if counts.total > cfg.max_affected_rows:
            raise WorkflowError(
                f"The dry run affected {counts.total} rows, above the limit of "
                f"{cfg.max_affected_rows} for {cfg.table_id}."
            )
        if sha256(str(request["CANONICAL_SQL"])) != request["SQL_SHA256"]:
            raise WorkflowError("Stored SQL failed its integrity check.")
        guard_dml(str(request["CANONICAL_SQL"]), cfg.source, cfg.allowed_reads)
        revision = self._ensure_run_current(request, "submit")
        self._record(
            request,
            Status.SUBMITTED,
            {"SUBMITTED_AT": NOW, "SUBMIT_NOTE": _clean_text(note, "Note") or None},
            "SUBMITTED",
            actor,
            details={"counts": counts.as_dict()},
            expected_revision=revision,
        )
        return {"request_id": request_id, "status": str(Status.SUBMITTED)}

    def cancel(self, request_id: str, actor: str, reason: str | None = None) -> dict[str, Any]:
        """Withdraw a request that has not started executing."""
        request = self._store.get(request_id)
        ensure_requester(str(request["REQUESTED_BY"]), actor, "cancel")
        self._record(
            request,
            Status.CANCELLED,
            {"CANCELLED_AT": NOW},
            "CANCELLED",
            actor,
            details={"reason": _clean_text(reason, "Reason")},
        )
        return {"request_id": request_id, "status": str(Status.CANCELLED)}

    def send_back(
        self, request_id: str, actor: str, comment: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Admin: return an approved, not yet executed request to review."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "send back")
        self._check_displayed_revision(request, expected_revision)
        if str(request["STATUS"]) != Status.APPROVED:
            raise WorkflowError("Only approved requests can be sent back for review.")
        comment_text = _clean_text(comment, "A comment", required=True)
        self._return_to_review(
            request,
            actor,
            f"Sent back by {actor}: {comment_text}",
            "SENT_BACK",
            {"comment": comment_text},
        )
        return {"request_id": request_id, "status": str(Status.SUBMITTED)}

    def _return_to_review(
        self,
        request: Mapping[str, Any],
        actor: str,
        reason: str,
        event: str,
        details: Mapping[str, Any],
        *,
        execution_values: Mapping[str, Any] | None = None,
    ) -> None:
        # Invalidate stale execution attempts even if another admin re-approves
        # with a different tolerance while the first screen or call is still open.
        revision = self._revision(request)
        self._record(
            request,
            Status.SUBMITTED,
            {
                **(execution_values or {}),
                "APPROVED_SHA256": None,
                "LAST_ERROR": reason,
                "REVISION": revision + 1,
                "RUN_REVISION": revision + 1,
            },
            event,
            actor,
            details=details,
            expected_revision=revision,
        )

    def drift(self, request_id: str, requester: str | None = None) -> dict[str, Any]:
        """Report changes to the source since the latest dry run / re-validation."""
        request = self._store.get(request_id)
        if requester is not None:
            ensure_requester(str(request["REQUESTED_BY"]), requester, "inspect drift for")
        cfg = self._request_config(request)
        report = self._drift(request, cfg)
        return {
            **report.as_dict(),
            "revision": self._revision(request),
            "source_fqn": str(cfg.source),
            "key_columns": list(cfg.key_columns),
            "revalidated": bool(request.get("REVAL_SNAPSHOT_NS")),
        }

    def revalidate(self, request_id: str, actor: str) -> dict[str, Any]:
        """Admin: re-run the submitted SQL on a fresh clone of the *current* source."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "re-validate")
        if str(request["STATUS"]) != Status.SUBMITTED:
            raise WorkflowError("Only submitted requests can be re-validated.")
        cfg = self._request_config(request)
        if sha256(str(request["CANONICAL_SQL"])) != request["SQL_SHA256"]:
            raise WorkflowError("Stored SQL failed its integrity check.")
        legacy = request.get("RUN_CONTEXT_VERSION") != DML_CONTEXT_VERSION
        guidance = "Ask the requester to open a new request and dry-run the original SQL."
        try:
            guarded = guard_dml(str(request["CANONICAL_SQL"]), cfg.source, cfg.allowed_reads)
            if legacy:
                original = guard_dml(
                    str(request.get("SQL_TEXT") or ""), cfg.source, cfg.allowed_reads
                )
                if original.canonical_sql != guarded.canonical_sql:
                    raise WorkflowError(
                        f"The original SQL now differs from the submitted statement. {guidance}"
                    )
        except GuardError as error:
            if legacy:
                raise WorkflowError(
                    f"The original developer SQL cannot be verified. {guidance}"
                ) from error
            raise
        with self._attempt(request, actor, "REVAL_SANDBOX_REVISION", "REVALIDATION_FAILED") as (
            revision,
            refs,
        ):
            run = self._sandboxed_run(
                cfg, guarded, refs, _sandbox_label(request_id, actor, cfg.source, "re-validation")
            )
            dev_counts = _counts(request.get("DRY_RUN_COUNTS"))
            matches = (
                run["counts"] == dev_counts
                and run["fingerprint"] is not None
                and run["fingerprint"] == request.get("DRY_RUN_FINGERPRINT")
            )
            result = {
                "request_id": request_id,
                "counts": run["counts"].as_dict(),
                "dry_run_counts": dev_counts.as_dict(),
                "same_effect": matches,
                "revision": revision,
                "query_id": run["query_id"],
                "elapsed_ms": run["elapsed_ms"],
            }
            self._record(
                request,
                None,
                {
                    "REVAL_SNAPSHOT_NS": run["snapshot_ns"],
                    "REVAL_COUNTS": run["counts"].as_dict(),
                    "REVAL_FINGERPRINT": run["fingerprint"],
                    "REVAL_MATCHES": matches,
                    "REVAL_BY": actor,
                    "REVAL_AT": NOW,
                    "RUN_CONTEXT_VERSION": DML_CONTEXT_VERSION,
                    "RUN_REVISION": revision,
                    "REVAL_SANDBOX_REVISION": revision,
                    "LAST_ERROR": None,
                },
                "REVALIDATED",
                actor,
                details=result,
                expected_revision=revision,
            )
            return result

    def review(
        self,
        request_id: str,
        actor: str,
        decision: str,
        comment: str,
        tolerance_pct: float = 0.0,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Admin: approve or reject. Approval requires no drift since the latest sandbox run."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "review")
        self._check_displayed_revision(request, expected_revision)
        comment_text = _clean_text(comment, "A review comment", required=True)
        decision = decision.upper()
        if decision == "REJECT":
            self._record(
                request,
                Status.REJECTED,
                {"REVIEWED_BY": actor, "REVIEWED_AT": NOW, "REVIEW_COMMENT": comment_text},
                "REJECTED",
                actor,
                details={"comment": comment_text},
                expected_revision=self._revision(request),
            )
            return {"request_id": request_id, "status": str(Status.REJECTED)}
        if decision != "APPROVE":
            raise WorkflowError("decision must be APPROVE or REJECT.")
        if not 0 <= float(tolerance_pct) <= 100:
            raise WorkflowError("tolerance_pct must be between 0 and 100.")
        cfg = self._request_config(request)
        for column in ("DRY_RUN_COUNTS", "REVAL_COUNTS"):
            total = _counts(request.get(column)).total
            if total > cfg.max_affected_rows:
                raise WorkflowError(
                    f"{total} affected rows exceed the limit of {cfg.max_affected_rows} "
                    f"for {cfg.table_id}."
                )
        revision = self._ensure_run_current(request, "approve")
        if sha256(str(request["CANONICAL_SQL"])) != request["SQL_SHA256"]:
            raise WorkflowError("Stored SQL failed its integrity check.")
        try:
            report = self._check_review_baseline(request, cfg)
        except _DefinitionChangedError as error:
            if request["STATUS"] == Status.SUBMITTED:
                self._record(
                    request,
                    None,
                    {"LAST_ERROR": error.reason},
                    "APPROVAL_BLOCKED",
                    actor,
                    details=error.details,
                    expected_revision=revision,
                )
            raise
        self._record(
            request,
            Status.APPROVED,
            {
                "REVIEWED_BY": actor,
                "REVIEWED_AT": NOW,
                "REVIEW_COMMENT": comment_text,
                "APPROVED_SHA256": request["SQL_SHA256"],
                "TOLERANCE_PCT": float(tolerance_pct),
            },
            "APPROVED",
            actor,
            details={"comment": comment_text, "drift": report.as_dict()},
            expected_revision=revision,
        )
        return {"request_id": request_id, "status": str(Status.APPROVED)}

    def execute(
        self, request_id: str, actor: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Admin: run the approved statement on the source, transactionally and guarded."""
        request = self._store.get(request_id)
        ensure_not_requester(str(request["REQUESTED_BY"]), actor, "execute")
        self._check_displayed_revision(request, expected_revision)
        if str(request["STATUS"]) != Status.APPROVED:
            raise WorkflowError("Only approved requests can be executed.")
        cfg = self._request_config(request)
        canonical = str(request["CANONICAL_SQL"])
        if sha256(canonical) != request.get("APPROVED_SHA256"):
            raise WorkflowError("The SQL no longer matches what was approved.")
        guarded = guard_dml(canonical, cfg.source, cfg.allowed_reads)

        revision = self._ensure_run_current(request, "execute")
        expected = _counts(request.get("REVAL_COUNTS") or request.get("DRY_RUN_COUNTS"))
        tolerance = float(request.get("TOLERANCE_PCT") or 0.0)
        attempt = _DmlAttempt()
        result: Row | None = None
        claimed = False
        self._store.begin()
        try:
            self._transition(request, Status.EXECUTING, {"EXECUTED_BY": actor}, revision)
            claimed = True
            self._store.audit(request_id, "EXECUTING", actor, {})
            self._check_review_baseline(request, cfg)
            self._source_columns(cfg)
            actual, query_id, elapsed_ms = self._run_dml(guarded.render_for(cfg.source), attempt)
            if not within_tolerance(expected, actual, tolerance):
                raise _ExecutionCountError(
                    f"Rolled back: affected {actual.as_dict()} but approval expected "
                    f"{expected.as_dict()} (tolerance {tolerance}%)."
                )
            if actual.total > cfg.max_affected_rows:
                raise _ExecutionCountError(
                    f"Rolled back: affected {actual.total} rows, above the limit of "
                    f"{cfg.max_affected_rows} for {cfg.table_id}."
                )
            # Read committed allows another writer to finish between a preflight
            # query and the source DML. Check committed history through a timestamp
            # after the DML, while the source mutation can still be rolled back.
            self._check_review_baseline(request, cfg)
            result = {
                "request_id": request_id,
                "status": str(Status.EXECUTED),
                "counts": actual.as_dict(),
                "query_id": query_id,
                "elapsed_ms": elapsed_ms,
            }
            self._transition(
                {**request, "STATUS": str(Status.EXECUTING)},
                Status.EXECUTED,
                {
                    "EXEC_COUNTS": actual.as_dict(),
                    "EXEC_QUERY_ID": query_id,
                    "EXEC_ELAPSED_MS": elapsed_ms,
                    "EXECUTED_AT": NOW,
                },
                revision,
            )
            self._store.audit(request_id, "EXECUTED", actor, result)
            self._store.commit()
            return result
        except Exception as error:
            self._rollback(error)
            if result is not None:
                current = self._store.get(request_id)
                if (
                    current["STATUS"] == Status.EXECUTED
                    and current.get("EXEC_QUERY_ID") == result["query_id"]
                ):
                    return result  # The COMMIT succeeded and only its response was lost.
            if claimed:
                self._record_execution_error(request, actor, error, attempt, revision)
                if isinstance(error, _RevalidationRequiredError):
                    raise WorkflowError(
                        f"{error.reason} The request went back to SUBMITTED for re-validation."
                    ) from error
            raise

    def _record_execution_error(
        self,
        request: Mapping[str, Any],
        actor: str,
        error: Exception,
        attempt: _DmlAttempt,
        revision: int,
    ) -> None:
        evidence = attempt.details()
        exec_values = {
            "EXEC_QUERY_ID": attempt.query_id or None,
            "EXEC_ELAPSED_MS": attempt.elapsed_ms,
            "EXEC_COUNTS": evidence.get("counts"),
        }
        if isinstance(error, _RevalidationRequiredError):
            self._return_to_review(
                request,
                actor,
                error.reason,
                "DRIFT_BLOCKED",
                {**error.details, **evidence},
                execution_values=exec_values,
            )
            return
        values: Row = {
            **exec_values,
            "LAST_ERROR": str(error)[:MAX_TEXT],
            "EXECUTED_AT": NOW,
            "EXECUTED_BY": actor,
        }
        self._record(
            request,
            Status.FAILED,
            values,
            "EXECUTION_ROLLED_BACK"
            if isinstance(error, _ExecutionCountError)
            else "EXECUTION_FAILED",
            actor,
            details={"error": str(error), **evidence},
            expected_revision=revision,
        )

    def _drop_attempt(self, request: Mapping[str, Any], names: SandboxNames) -> None:
        self._sql.query(f"DROP STREAM IF EXISTS {self._ref(request, names.stream).sql}")
        for table in (names.clone, names.diff):
            self._sql.query(f"DROP TABLE IF EXISTS {self._ref(request, table).sql}")

    def _discard_initial_sandbox(self, request_id: str, names: SandboxNames, actor: str) -> None:
        request = self._store.get(request_id)
        try:
            self._drop_attempt(request, names)
        except Exception as error:
            details = {"error": str(error), "clone": names.clone}
            # A cleanup that already visited the base might still be marking it
            # dropped. Invalidate that claim and make the terminal row retryable.
            while request["STATUS"] in TERMINAL_STATUSES:
                revision = self._revision(request)
                with self._transaction():
                    if self._store.update(
                        request_id,
                        {"SANDBOX_DROPPED_AT": None, "REVISION": revision + 1},
                        str(request["STATUS"]),
                        revision,
                    ):
                        self._store.audit(request_id, "SANDBOX_DROP_FAILED", actor, details)
                        return
                request = self._store.get(request_id)
            self._store.audit(request_id, "SANDBOX_DROP_FAILED", actor, details)

    def _expire_idle_drafts(self, days: int, actor: str, request_id: str | None) -> list[Row]:
        expired = []
        for request in self._store.idle_drafts(days, request_id):
            try:
                self._record(
                    request,
                    Status.CANCELLED,
                    {"CANCELLED_AT": NOW, "LAST_ERROR": f"Draft expired after {days} idle days."},
                    "DRAFT_EXPIRED",
                    actor,
                    details={"idle_days": days},
                    expected_revision=self._revision(request),
                )
            except WorkflowError:
                if self._rollback_uncertain:
                    raise
                continue  # A user edited or submitted it after the candidate query.
            expired.append({**request, "STATUS": str(Status.CANCELLED)})
        return expired

    def cleanup(
        self,
        retention_days: int,
        actor: str,
        request_id: str | None = None,
        idle_draft_days: int = 0,
    ) -> dict[str, Any]:
        """Drop sandbox objects of final requests older than ``retention_days``.

        One request whose objects cannot be dropped is audited and skipped, so it
        does not block the cleanup of every other request night after night.
        """
        if min(retention_days, idle_draft_days) < 0:
            raise WorkflowError("Cleanup retention and idle draft days must be 0 or more.")
        dropped, failed = 0, 0
        candidates = self._store.cleanup_candidates(retention_days, request_id)
        expired = (
            self._expire_idle_drafts(idle_draft_days, actor, request_id) if idle_draft_days else []
        )
        for request in [*candidates, *expired]:
            request_id = str(request["REQUEST_ID"])
            names = self._names(request)
            revision = self._revision(request)
            try:
                self._drop_attempt(request, names)
                # Legacy re-validations used the unversioned _RV triple.
                self._drop_attempt(request, SandboxNames(names.reval_clone))
                for attempt in range(1, revision + 1):
                    self._drop_attempt(request, names.for_revision(attempt))
                self._record(
                    request,
                    None,
                    {"SANDBOX_DROPPED_AT": NOW, "REVISION": revision + 1},
                    "SANDBOX_DROPPED",
                    actor,
                    details={},
                    expected_revision=revision,
                )
                dropped += 1
            except Exception as error:  # Report per request and continue.
                if self._rollback_uncertain:
                    raise
                with contextlib.suppress(Exception):
                    if self._store.get(request_id).get("SANDBOX_DROPPED_AT"):
                        continue  # A concurrent cleanup finished first.
                failed += 1
                with contextlib.suppress(Exception):
                    self._store.audit(
                        request_id, "SANDBOX_DROP_FAILED", actor, {"error": str(error)}
                    )
        return {"dropped": dropped, "failed": failed, "expired": len(expired)}


#: Operations of ``CORE.SP_DEV_API`` (developer app) and ``CORE.SP_ADMIN_API``
#: (admin app and the cleanup task). Only ``drift`` (read-only) is in both.
DEVELOPER_OPS = frozenset({"create_request", "dry_run", "submit", "cancel", "drift"})
ADMIN_OPS = frozenset({"drift", "revalidate", "review", "execute", "send_back", "cleanup"})


def _actor(args: Mapping[str, Any]) -> str:
    actor = _clean_text(args.get("actor"), "actor", required=True)
    if len(actor) > 255:
        raise WorkflowError("actor is too long.")
    return actor


def dispatch(
    runner: DmlRunner,
    core_schema: str,
    op: str,
    raw_args: Any,
    allowed_ops: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Entry point of the ``CORE.SP_*_API(op, args)`` procedures; never raises.

    ``allowed_ops`` is the operation set of the calling procedure, so the developer
    app's procedure cannot review or execute even if its caller is wrong.
    """
    try:
        if allowed_ops is not None and op not in allowed_ops:
            raise WorkflowError(f"Operation {op!r} is not available through this procedure.")
        args: Row = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        engine = Engine(runner, SqlRequestStore(runner, core_schema))
        actor = _actor(args)
        rid = str(args.get("request_id") or "")
        operations: dict[str, Callable[[], dict[str, Any]]] = {
            "create_request": lambda: engine.create_request(str(args.get("table_id")), actor),
            "dry_run": lambda: engine.dry_run(
                rid, actor, str(args.get("sql") or ""), args.get("nl_prompt")
            ),
            "submit": lambda: engine.submit(
                rid, actor, args.get("note"), args.get("sql"), args.get("expected_revision")
            ),
            "cancel": lambda: engine.cancel(rid, actor, args.get("reason")),
            "drift": lambda: engine.drift(
                rid, requester=actor if allowed_ops == DEVELOPER_OPS else None
            ),
            "revalidate": lambda: engine.revalidate(rid, actor),
            "review": lambda: engine.review(
                rid,
                actor,
                str(args.get("decision") or ""),
                str(args.get("comment") or ""),
                _number(args, "tolerance_pct", 0.0),
                expected_revision=args.get("expected_revision"),
            ),
            "execute": lambda: engine.execute(rid, actor, args.get("expected_revision")),
            "send_back": lambda: engine.send_back(
                rid,
                actor,
                str(args.get("comment") or ""),
                expected_revision=args.get("expected_revision"),
            ),
            "cleanup": lambda: engine.cleanup(
                _days(args, "retention_days", 7),
                actor,
                _clean_text(args["request_id"], "request_id", required=True)
                if "request_id" in args
                else None,
                _days(args, "idle_draft_days", 0),
            ),
        }
        if op not in operations:
            raise WorkflowError(f"Unknown operation {op!r}.")
        return {"ok": True, "result": operations[op]()}
    except Exception as error:  # surfaced to the UI; the audit log keeps the details
        return {"ok": False, "error": str(error), "error_type": type(error).__name__}

# --------------------------------------------------------------------- handler
_CORE_SCHEMA = '"{{ app_db }}"."CORE"'


def handler(session, op, args):
    """Stored-procedure handler: dispatch one admin operation with owner's rights."""
    return dispatch(SnowparkRunner(session), _CORE_SCHEMA, op, args, ADMIN_OPS)
$$;
