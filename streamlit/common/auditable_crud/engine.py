"""Change-request workflow, shared by the two owner's-rights procedures.

Every state change re-validates its preconditions server-side: the apps are
convenience UIs, this module is the authority. Status changes use
compare-and-set (``UPDATE ... WHERE STATUS = <expected>``) so double clicks or
two admins acting at once cannot both win.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from auditable_crud.config import TableConfig
from auditable_crud.diff import DiffCounts, counts_sql, fingerprint_sql, materialize_sql
from auditable_crud.drift import DriftReport, build_report, drift_counts_sql, drift_overlap_sql
from auditable_crud.naming import (
    SandboxNames,
    build_sandbox_names,
    information_schema,
    sql_string_literal,
)
from auditable_crud.runner import DML_CONTEXT_VERSION, DmlRunner, Row, scalar
from auditable_crud.sql_guard import (
    GuardedStatement,
    GuardError,
    TableRef,
    guard_dml,
    has_sequence_default,
)
from auditable_crud.state import (
    EDITABLE_STATUSES,
    TERMINAL_STATUSES,
    Status,
    WorkflowError,
    ensure_not_requester,
    ensure_requester,
    ensure_transition,
)
from auditable_crud.store import NOW, RequestStore, SqlRequestStore

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
