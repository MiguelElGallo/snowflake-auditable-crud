"""Streamlit building blocks shared by the developer and admin apps.

Page scripts stay thin; anything reused (repository wiring, table picker,
diff browser, drift panel, API error handling) lives here.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from decimal import Decimal
from numbers import Integral
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
import streamlit as st
from PIL import __version__ as pillow_version
from streamlit.connections import SnowflakeConnection

from auditable_crud.config import TableConfig
from auditable_crud.diff import ChangeKind
from auditable_crud.drift import DriftClass
from auditable_crud.repository import (
    PAGE_SIZE,
    ApiError,
    Repository,
    RequestView,
    SandboxObject,
    core_schema_for,
)
from auditable_crud.runner import SnowparkRunner
from auditable_crud.state import TERMINAL_STATUSES, Status, same_user

if TYPE_CHECKING:
    from snowflake.snowpark import Session

REPOSITORY_KEY = "repository"
ACTOR_KEY = "actor"

STATUS_COLORS: dict[str, str] = {
    Status.DRAFT: "gray",
    Status.DRY_RUN_OK: "blue",
    Status.SUBMITTED: "orange",
    Status.APPROVED: "violet",
    Status.EXECUTING: "orange",
    Status.EXECUTED: "green",
    Status.FAILED: "red",
    Status.REJECTED: "red",
    Status.CANCELLED: "gray",
}
DRIFT_COLORS = {DriftClass.NONE: "green", DriftClass.DISJOINT: "orange", DriftClass.OVERLAP: "red"}
# Icons make statuses distinguishable without relying on color.
STATUS_ICONS: dict[str, str] = {
    Status.DRAFT: ":material/edit:",
    Status.DRY_RUN_OK: ":material/science:",
    Status.SUBMITTED: ":material/send:",
    Status.APPROVED: ":material/verified:",
    Status.EXECUTING: ":material/sync:",
    Status.EXECUTED: ":material/check_circle:",
    Status.FAILED: ":material/error:",
    Status.REJECTED: ":material/block:",
    Status.CANCELLED: ":material/cancel:",
}
# Human labels for the request list columns.
REQUEST_COLUMNS = {
    "REQUEST_ID": st.column_config.TextColumn("Request", width="small"),
    "TABLE_ID": st.column_config.TextColumn("Table"),
    "STATUS": st.column_config.TextColumn("Status"),
    "REQUESTED_BY": st.column_config.TextColumn("Requested by"),
    "STATEMENT_KIND": st.column_config.TextColumn("Kind"),
    "ROWS_AFFECTED": st.column_config.NumberColumn("Rows"),
    "CREATED_AT": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD HH:mm"),
    "UPDATED_AT": st.column_config.DatetimeColumn("Updated", format="YYYY-MM-DD HH:mm"),
    "YOURS": st.column_config.CheckboxColumn(
        "Yours", help="You submitted it, so you cannot review it"
    ),
    "WAITING_H": st.column_config.NumberColumn("Waiting (h)"),
}
# Characters with meaning in Markdown or in Streamlit directives (:red[...], :material/...:).
_MARKDOWN_SPECIAL = frozenset("\\`*_{}[]()#+-.!|<>~:$")


def safe_md(value: object) -> str:
    """Escape user-supplied text so Markdown renders it literally.

    Notes, comments, prompts and error messages are written by users; without
    escaping, they could plant links, images or colored "official" text in
    another user's (e.g. an admin's) view.
    """
    return "".join(f"\\{ch}" if ch in _MARKDOWN_SPECIAL else ch for ch in str(value or ""))


# ----------------------------------------------------------------------- wiring
class _ViewerSnowflakeConnection(SnowflakeConnection):
    """Keep the owner-rights Snowflake connection private to one Streamlit viewer session."""

    _snowpark_session: Session | None = None

    @classmethod
    def scope(cls) -> Literal["session"]:
        """Release the connection when the viewer disconnects."""
        return "session"

    def session(self) -> Session:
        """Reuse the same Snowpark session for the lifetime of this connection."""
        if self._snowpark_session is None:
            self._snowpark_session = super().session()
        return self._snowpark_session

    def close(self) -> None:
        """Release the Snowpark session from its process-wide registry when the viewer leaves."""
        try:
            if self._snowpark_session is not None:
                self._snowpark_session.close()
        finally:
            self._snowpark_session = None
            super().close()


def get_repository(procedure: str | None = None) -> Repository:
    """Bind the app repository to the live connection on every entrypoint run."""
    repo: Repository | None = st.session_state.get(REPOSITORY_KEY)
    # Pages use the entrypoint's repository. Tests can also inject one without a SQL session.
    if repo is not None and (procedure is None or "snowflake_session_id" not in st.session_state):
        return repo
    if procedure is None:
        raise RuntimeError("get_repository(procedure) must be called by the app first.")

    # A reconnect can retain widget state after Streamlit releases the connection.
    session = st.connection("snowflake", type=_ViewerSnowflakeConnection).session()
    if repo is not None and st.session_state.get("_repository_session") is session:
        return repo
    database = str(session.get_current_database() or "").strip('"')
    if not database:
        st.error("No current database: deploy the app with DCM or set one on the connection.")
        st.stop()
    repo = Repository(SnowparkRunner(session), core_schema_for(database), procedure)
    st.session_state[REPOSITORY_KEY] = repo
    st.session_state["_repository_session"] = session
    st.session_state["snowflake_session_id"] = session.session_id
    return repo


LOCAL_DEV_ENV = "AC_LOCAL_DEV"


def current_actor(repo: Repository) -> str:
    """Return the Snowflake user name of the person viewing the app.

    In Streamlit in Snowflake the app session runs as the app *owner*, so
    ``CURRENT_USER()`` is the same for everyone; only ``st.user.user_name``
    identifies the viewer. Falling back to the session user would make every
    viewer the same actor and defeat the no-self-approval rule, so the app
    stops instead. ``AC_LOCAL_DEV=1`` allows the fallback for local runs, where
    the session user *is* the developer.
    """
    if ACTOR_KEY not in st.session_state:
        user_name = str((st.user.get("user_name") if st.user else None) or "").strip()
        if not user_name and os.environ.get(LOCAL_DEV_ENV) == "1":
            user_name = repo.current_user()
        if not user_name:
            st.error(
                "Could not determine who you are (st.user has no user name), so changes "
                "cannot be attributed. Open the app from Snowsight.",
                icon=":material/no_accounts:",
            )
            st.stop()
        st.session_state[ACTOR_KEY] = user_name
    return str(st.session_state[ACTOR_KEY])


def call_api(
    repo: Repository,
    op: str,
    success: str | None = None,
    *,
    refresh_on_error: bool = False,
    **args: Any,
) -> dict[str, Any]:
    """Show procedure errors, reloading the request for actions that can change state on failure."""
    try:
        result = repo.call(op, actor=current_actor(repo), **args)
    except ApiError as error:
        if refresh_on_error:
            st.session_state[f"action_error_{args['request_id']}"] = str(error)
            st.rerun()
        st.error(safe_md(error), icon=":material/error:")
        return {}
    if success:
        st.toast(success, icon=":material/check_circle:")
    return result


def load_request(
    repo: Repository,
    state_key: str,
    back_page: str,
    back_label: str,
    *,
    requested_by: str | None = None,
) -> tuple[RequestView, TableConfig]:
    """Load the request in ``st.session_state[state_key]`` and its table config.

    A request that no longer exists, or whose table is no longer managed, gets a
    short explanation and a link back instead of a traceback.
    """
    try:
        request = repo.request(str(st.session_state[state_key]))
        if requested_by is not None and not same_user(str(request["REQUESTED_BY"]), requested_by):
            raise ApiError("This request belongs to another viewer.")
    except ApiError:
        st.session_state[state_key] = None
        st.warning(
            "That request no longer exists or is unavailable to you.", icon=":material/search_off:"
        )
        st.page_link(back_page, label=back_label, icon=":material/arrow_back:")
        st.stop()
    return request, _table_or_stop(repo, request, back_page, back_label)


def load_request_by_id(repo: Repository, request_id: str) -> tuple[RequestView, TableConfig | None]:
    """Load a request picked from a list; its table may no longer be managed (read-only)."""
    try:
        request = repo.request(request_id)
    except ApiError:
        st.warning("That request no longer exists.", icon=":material/search_off:")
        st.stop()
    cfg = table_lookup(repo).get(str(request["TABLE_ID"]))
    return request, cfg if cfg and str(cfg.source) == request["SOURCE_FQN"] else None


def _table_or_stop(
    repo: Repository, request: RequestView, back_page: str, back_label: str
) -> TableConfig:
    cfg = table_lookup(repo).get(str(request["TABLE_ID"]))
    if cfg is None:
        st.error(
            f"Table **{safe_md(request['TABLE_ID'])}** is no longer managed by Auditable CRUD, "
            "so this request can no longer be worked on.",
            icon=":material/table_view:",
        )
        st.page_link(back_page, label=back_label, icon=":material/arrow_back:")
        st.stop()
    if str(cfg.source) != request["SOURCE_FQN"]:
        st.error("This table ID now points to a different source. Open a new request for it.")
        st.page_link(back_page, label=back_label, icon=":material/arrow_back:")
        st.stop()
    return cfg


def stage_counts(request: RequestView, audit: pd.DataFrame) -> None:
    """Show each stage's change count, duration and Snowflake DML query ID."""
    revalidation: dict[str, Any] = {}
    if request.counts("REVAL_COUNTS") and not audit.empty:
        revision = request["REVAL_SANDBOX_REVISION"]
        events = audit.loc[audit["EVENT"] == "REVALIDATED", "DETAILS"]
        for raw in reversed(events.tolist()):
            details = json.loads(raw) if isinstance(raw, str) else raw
            # Older events have a duration but no revision or query ID.
            if isinstance(details, dict) and details.get("revision", revision) == revision:
                revalidation = details
                break
    if request.status == Status.EXECUTED:
        source_stage = "Execution"
    elif request.counts("EXEC_COUNTS"):
        source_stage = "Rolled back"
    else:
        source_stage = "Failed execution"
    rows = [
        {"Stage": label, **request.counts(column), "Query ID": query_id, "Elapsed (ms)": elapsed}
        for label, column, query_id, elapsed in (
            (
                "Dry run",
                "DRY_RUN_COUNTS",
                request["DRY_RUN_QUERY_ID"],
                request["DRY_RUN_ELAPSED_MS"],
            ),
            (
                "Re-validation",
                "REVAL_COUNTS",
                revalidation.get("query_id"),
                revalidation.get("elapsed_ms"),
            ),
            (
                source_stage,
                "EXEC_COUNTS",
                request["EXEC_QUERY_ID"],
                request["EXEC_ELAPSED_MS"],
            ),
        )
        if request.counts(column) or (column == "EXEC_COUNTS" and (query_id or elapsed is not None))
    ]
    if not rows:
        st.caption("No dry run yet.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def confirm_button(
    label: str,
    *,
    key: str,
    prompt: str,
    confirm_label: str,
    icon: str | None = None,
    primary: bool = False,
    disabled: bool = False,
) -> bool:
    """Two-step button for irreversible actions; returns True once confirmed."""
    flag = f"confirm_{key}"
    if disabled:
        st.session_state.pop(flag, None)
    if not st.session_state.get(flag):
        if st.button(
            label,
            key=f"{key}_ask",
            icon=icon,
            type="primary" if primary else "secondary",
            disabled=disabled,
        ):
            st.session_state[flag] = True
            st.rerun()
        return False
    with st.container(border=True):
        st.warning(prompt, icon=":material/warning:")
        with st.container(horizontal=True):
            confirmed = st.button(confirm_label, key=f"{key}_yes", type="primary", icon=icon)
            if st.button("Cancel", key=f"{key}_no"):
                st.session_state[flag] = False
                st.rerun()
    if confirmed:
        st.session_state[flag] = False
    return confirmed


# ---------------------------------------------------------------------- display
def status_badge(status: str) -> None:
    """Colored badge with an icon for a request status."""
    st.badge(
        status_label(status),
        color=STATUS_COLORS.get(status, "gray"),  # type: ignore[arg-type]
        icon=STATUS_ICONS.get(status),
    )


def status_label(status: str) -> str:
    """``DRY_RUN_OK`` -> ``Dry Run Ok``."""
    return str(status).replace("_", " ").title()


def signed_in_caption(repo: Repository) -> None:
    """Show the audit actor and let the viewer inspect the running app and its SQL session."""
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(
            f":material/person: Signed in as **{safe_md(current_actor(repo))}**", width="content"
        )
        with st.popover("Runtime details", type="tertiary"):
            st.text(
                f"Streamlit {st.__version__}\nPillow {pillow_version}\n"
                f"Snowflake session {st.session_state.get('snowflake_session_id', 'unavailable')}"
            )


def count_metrics(counts: dict[str, int], row_count: int | None = None, label: str = "") -> None:
    """Show inserted / updated / deleted / total tiles, with share of the table when known."""
    with st.container(horizontal=True):
        st.metric(f"{label}Inserted", counts.get("inserted", 0), border=True)
        st.metric(f"{label}Updated", counts.get("updated", 0), border=True)
        st.metric(f"{label}Deleted", counts.get("deleted", 0), border=True)
        total = counts.get("total", 0)
        share = f"{total / row_count:.1%} of the table's {row_count:,} rows" if row_count else None
        st.metric(f"{label}Total affected", total, help=share, border=True)


def table_lookup(repo: Repository) -> dict[str, TableConfig]:
    """Return unambiguous managed tables and show conflicting manifest IDs."""
    tables = repo.tables()
    counts = Counter(t.table_id for t in tables)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        st.error("These table IDs occur more than once in the manifest: " + ", ".join(duplicates))
    return {t.table_id: t for t in tables if counts[t.table_id] == 1}


def table_stats_rows(repo: Repository, cfg: TableConfig) -> int | None:
    """Return the source table's row count, if Snowflake reports one."""
    value = repo.table_stats(cfg).get("ROW_COUNT")
    return None if value is None else int(value)


def table_picker(
    repo: Repository,
    *,
    button_label: str,
    key: str,
    open_requests: dict[str, int],
    open_drafts: dict[str, int],
) -> TableConfig | None:
    """First screen: one card per managed table. Returns the table whose button was clicked."""
    tables = table_lookup(repo).values()
    if not tables:
        st.info(
            "No tables are available for changes yet. Ask your platform admin to add one.",
            icon=":material/table_view:",
        )
        return None
    chosen: TableConfig | None = None
    columns = st.columns(min(len(tables), 3))
    for index, cfg in enumerate(tables):
        stats = repo.table_stats(cfg)
        rows = stats.get("ROW_COUNT")
        with columns[index % len(columns)].container(border=True, height="stretch"):
            st.subheader(cfg.table_id, anchor=False)
            st.caption(cfg.description or str(cfg.source))
            st.markdown(
                f":material/table: `{cfg.source}`  \n"
                f":material/key: {', '.join(cfg.key_columns)}  \n"
                f":material/science: clones in `{cfg.clone_database}.{cfg.clone_schema}`"
            )
            with st.container(horizontal=True):
                st.metric("Rows", "n/a" if rows is None else f"{int(rows):,}")
                st.metric("Max rows per change", f"{cfg.max_affected_rows:,}")
                st.metric("My open requests", open_requests.get(cfg.table_id, 0))
            drafts = open_drafts.get(cfg.table_id, 0)
            if drafts:
                st.caption(
                    f":material/edit: You already have {drafts} unsubmitted request"
                    f"{'s' if drafts > 1 else ''} here; resume under My requests."
                )
            if st.button(
                button_label, key=f"{key}_{cfg.table_id}", type="primary", width="stretch"
            ):
                chosen = cfg
    return chosen


def _changed_columns(frame: pd.DataFrame) -> pd.Series:
    """Per row, the names of columns whose before and after values differ."""
    names: list[pd.Series] = []
    for column in frame.columns:
        if str(column).endswith(" (after)"):
            base = str(column)[: -len(" (after)")]
            before = f"{base} (before)"
            if before in frame.columns:
                changed = _different_values(frame[before], frame[column])
                names.append(changed.map({True: base, False: ""}))
    if not names:
        return pd.Series("", index=frame.index)
    joined = pd.concat(names, axis=1).apply(lambda r: ", ".join(v for v in r if v), axis=1)
    return joined.astype(str)


def _different_values(before: pd.Series, after: pd.Series) -> pd.Series:
    """Match nulls to nulls while keeping SQL text and numeric values typed."""
    return ~(before.eq(after).fillna(False) | (before.isna() & after.isna()))


def _highlight_changes(frame: pd.DataFrame) -> pd.DataFrame:
    """CSS for before/after frames: highlight cells whose value changed."""
    styles = pd.DataFrame("", index=frame.index, columns=frame.columns)
    for column in frame.columns:
        if not str(column).endswith(" (after)"):
            continue
        before = str(column)[: -len(" (after)")] + " (before)"
        if before in frame.columns:
            changed = _different_values(frame[before], frame[column])
            styles.loc[changed, column] = "background-color: rgba(255, 196, 0, 0.35)"
            styles.loc[changed, before] = "color: rgba(128, 128, 128, 0.9)"
    return styles


def display_table_values(frame: pd.DataFrame) -> pd.DataFrame:
    """Render exact decimals and integers beyond the browser's safe range as text."""
    shown = frame.copy()
    for name, values in frame.items():
        if any(
            isinstance(value, Decimal) or (isinstance(value, Integral) and abs(int(value)) >= 2**53)
            for value in values
        ):
            shown[name] = values.astype("string")
    return shown


def diff_browser(repo: Repository, request: RequestView, *, which: SandboxObject, key: str) -> bool:
    """Browse exactly the rows a dry run changed (from the materialized stream)."""
    if request["SANDBOX_DROPPED_AT"]:
        st.caption(
            "Sandbox cleanup removed the row-level change set. "
            "The counts and audit trail remain available."
        )
        return False
    if not request.counts("DRY_RUN_COUNTS"):
        st.caption("No dry run has been recorded.")
        return False
    diff = request.sandbox(which)
    counts = request.counts("REVAL_COUNTS" if which == "reval_diff" else "DRY_RUN_DIFF_COUNTS")
    columns = repo.diff_columns(diff)
    if not columns:
        st.warning("The saved change set is unavailable. Check the audit trail for its history.")
        return False
    labels = {
        ChangeKind.UPDATED: f"Updated ({counts.get('updated', 0)})",
        ChangeKind.INSERTED: f"Inserted ({counts.get('inserted', 0)})",
        ChangeKind.DELETED: f"Deleted ({counts.get('deleted', 0)})",
    }
    widget_key = f"{key}_{request.request_id}_{request.revision}"
    with st.container(horizontal=True, vertical_alignment="bottom"):
        # Radio (not segmented_control) keeps the page testable with Streamlit 1.52 AppTest.
        kind = st.radio(
            "Change type",
            list(labels),
            index=next((i for i, k in enumerate(labels) if counts.get(k)), 0),
            format_func=lambda k: labels[k],
            horizontal=True,
            key=f"{widget_key}_kind",
        )
        kind = kind or ChangeKind.UPDATED
        pages = max(1, math.ceil(counts.get(str(kind), 0) / PAGE_SIZE))
        page = st.number_input(
            f"Page (of {pages})",
            min_value=1,
            max_value=pages,
            value=1,
            step=1,
            key=f"{widget_key}_{kind}_page",
            width=120,
        )
    frame = repo.diff_page(diff, kind, columns, int(page) - 1)
    empty_run = bool(counts) and counts.get("total", sum(counts.get(k, 0) for k in labels)) == 0
    if frame.empty:
        if empty_run:
            st.caption("This run recorded no row changes.")
        elif counts.get(str(kind)):
            st.warning(f"The saved change set returned no {kind} rows on this page.")
        else:
            st.caption(f"No {kind} rows.")
    elif kind is ChangeKind.UPDATED:
        # The text column says what changed without relying on the highlight color.
        frame = frame.assign(**{"Changed columns": _changed_columns(frame)})
        frame = frame[["Changed columns", *[c for c in frame.columns if c != "Changed columns"]]]
        styles = _highlight_changes(frame)
        st.dataframe(
            display_table_values(frame).style.apply(lambda _: styles, axis=None), hide_index=True
        )
    else:
        st.dataframe(display_table_values(frame), hide_index=True)
    st.caption(
        f"Showing up to {PAGE_SIZE} rows per page from `{diff}` (a materialized copy of the "
        "stream on the clone)."
    )
    st.download_button(
        "Download this page as CSV",
        frame.to_csv(index=False).encode("utf-8"),
        file_name=f"{request.request_id}_{kind}_page{int(page)}.csv",
        mime="text/csv",
        icon=":material/download:",
        key=f"{widget_key}_download",
        disabled=frame.empty,
    )
    if kind is ChangeKind.UPDATED and counts.get("updated"):
        changes = repo.column_changes(diff, columns)
        chart = pd.DataFrame({"column": list(changes), "rows changed": list(changes.values())})
        st.markdown("**Rows changed per column**")
        st.bar_chart(chart, x="column", y="rows changed", horizontal=True)
    return empty_run if frame.empty else counts.get(str(kind), 0) > 0


def drift_panel(
    repo: Repository, request: RequestView, cfg: TableConfig, key: str
) -> dict[str, Any]:
    """Show what other users changed in the source since the latest sandbox run."""
    if request["SANDBOX_DROPPED_AT"] or not request.counts("DRY_RUN_COUNTS"):
        st.caption("A retained dry-run change set is required to compare drift.")
        return {}
    report = call_api(repo, "drift", request_id=request.request_id)
    if not report:
        return {}
    diff_name: SandboxObject = "reval_diff" if report.get("revalidated") else "diff"
    diff = request.sandbox(diff_name)
    if report.get("revision") != request.revision or report.get("diff_fqn") != str(diff):
        st.warning("The request changed. Refresh to see drift for its latest revision.")
        return {}
    if report.get("source_fqn") != str(cfg.source) or report.get("key_columns") != list(
        cfg.key_columns
    ):
        st.warning("The managed table configuration changed. Refresh to see its current drift.")
        return {}
    classification = DriftClass(report["classification"])
    baseline = "admin re-validation" if report.get("revalidated") else "developer dry run"
    with st.container(horizontal=True, vertical_alignment="center"):
        st.badge(
            classification.replace("_", " ").title(),
            color=DRIFT_COLORS[classification],  # type: ignore[arg-type]
            icon=":material/sync_problem:" if report["total"] else ":material/verified:",
        )
        st.caption(f"Changes to `{cfg.source}` committed since the {baseline}.")
    with st.container(horizontal=True):
        st.metric("Inserted since", report["inserted"], border=True)
        st.metric("Updated since", report["updated"], border=True)
        st.metric("Deleted since", report["deleted"], border=True)
        st.metric(
            "Overlap with this change",
            report["overlapping_keys"],
            help=f"Rows (by {', '.join(cfg.key_columns)}) changed by others AND by this request.",
            border=True,
        )
        if report.get("read_table_changes"):
            st.metric(
                "Changed in tables it reads",
                report["read_table_changes"],
                help="Rows changed in allowed read tables the statement uses; they can change "
                "its effect.",
                border=True,
            )
    if report["total"] and (report["inserted"] or report["updated"] or report["deleted"]):
        baseline_ns = int(report["baseline_ns"])
        rows = repo.drift_rows(cfg, baseline_ns, diff, end_ns=int(report["end_ns"]))
        st.dataframe(
            display_table_values(rows),
            hide_index=True,
            column_config={
                "AC_ACTION": st.column_config.TextColumn("Action"),
                "AC_ISUPDATE": st.column_config.CheckboxColumn("Part of update"),
                rows.attrs.get("overlap_column", "AC_OVERLAPS"): st.column_config.CheckboxColumn(
                    "Touched by this request"
                ),
            },
            key=f"{key}_drift_rows",
        )
    return report


def selected_request_id(frame: pd.DataFrame, key: str) -> str | None:
    """Show a selectable, labelled request table; return the selected REQUEST_ID, if any."""
    shown = frame.assign(STATUS=frame["STATUS"].map(status_label)) if "STATUS" in frame else frame
    # Streamlit selections are row positions. A filtered or reordered list must
    # start a fresh selection so a click cannot open a different request.
    displayed_ids = tuple(frame["REQUEST_ID"])
    event = st.dataframe(
        shown,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"{key}_{hash(displayed_ids)}",
        column_config=REQUEST_COLUMNS,
    )
    rows = event["selection"]["rows"]
    return str(displayed_ids[rows[0]]) if rows and 0 <= rows[0] < len(displayed_ids) else None


def audit_trail(events: pd.DataFrame) -> None:
    """Chronological audit events of a request."""
    st.dataframe(
        events,
        hide_index=True,
        column_config={"DETAILS": st.column_config.JsonColumn("Details", width="large")},
    )


def request_header(request: RequestView) -> None:
    """Status, ids and people involved."""
    with st.container(horizontal=True, vertical_alignment="center"):
        status_badge(request.status)
        st.caption(
            f"Request `{request.request_id}` on `{request['SOURCE_FQN']}` "
            f"by **{safe_md(request['REQUESTED_BY'])}**"
        )
    last_seen = st.session_state.get("displayed_revision")
    if last_seen and last_seen[0] == request.request_id and last_seen[1] != request.revision:
        st.info("The request has changed. Review the latest sandbox result before continuing.")
    st.session_state.displayed_revision = (request.request_id, request.revision)
    action_error = st.session_state.pop(f"action_error_{request.request_id}", None)
    if action_error:
        st.error(safe_md(action_error), icon=":material/error:")
    if request["LAST_ERROR"] and request.status not in TERMINAL_STATUSES - {Status.FAILED}:
        st.warning(f"Last error: {safe_md(request['LAST_ERROR'])}", icon=":material/warning:")
