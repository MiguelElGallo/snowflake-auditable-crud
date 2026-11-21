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

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from auditable_crud.naming import fqn, normalize_identifier

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
