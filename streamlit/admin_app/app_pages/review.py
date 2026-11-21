import pandas as pd
import streamlit as st

from auditable_crud import ui
from auditable_crud.repository import SandboxObject
from auditable_crud.state import Status, same_user

repo = ui.get_repository()
actor = ui.current_actor(repo)

if not st.session_state.get("review_id"):
    st.info("Pick a request from the review queue.", icon=":material/arrow_back:")
    st.page_link("app_pages/queue.py", label="Review queue", icon=":material/inbox:")
    st.stop()

request, cfg = ui.load_request(repo, "review_id", "app_pages/queue.py", "Review queue")
rid = request.request_id
own_request = same_user(str(request["REQUESTED_BY"]), actor)
ui.request_header(request)
if own_request:
    st.warning(
        "You submitted this request, so another admin must re-validate, approve, reject or "
        "execute it. To withdraw it, cancel it in the developer app.",
        icon=":material/block:",
    )
if request["SUBMIT_NOTE"]:
    st.info(
        f"**{ui.safe_md(request['REQUESTED_BY'])}:** {ui.safe_md(request['SUBMIT_NOTE'])}",
        icon=":material/chat:",
    )

st.code(str(request["CANONICAL_SQL"] or request["SQL_TEXT"] or ""), language="sql", wrap_lines=True)
if request["NL_PROMPT"]:
    st.caption(f":material/auto_awesome: Recorded prompt: {ui.safe_md(request['NL_PROMPT'])}")

stats = repo.table_stats(cfg)
row_count = None if stats.get("ROW_COUNT") is None else int(stats["ROW_COUNT"])
baseline = request["REVAL_SNAPSHOT_NS"] or request["SNAPSHOT_NS"]
retention = stats.get("RETENTION_TIME")
if baseline and retention is not None and request.status in (Status.SUBMITTED, Status.APPROVED):
    snapshot = pd.Timestamp(int(baseline), tz="UTC")
    if snapshot < pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(retention)):
        st.warning(
            f"The latest sandbox snapshot is older than this table's {retention}-day Time Travel "
            "retention. Re-validate on current data before approval. An approved request can be "
            "returned to review below.",
            icon=":material/history:",
        )
ui.count_metrics(request.counts("DRY_RUN_COUNTS"), row_count, label="Dry run: ")
st.caption(
    f"Developer dry run took {request['DRY_RUN_ELAPSED_MS']} ms "
    f"(query `{request['DRY_RUN_QUERY_ID']}`) on a clone of the table."
)

views = ["Change set", "Audit trail"]
if request.status in (Status.SUBMITTED, Status.APPROVED):
    views.insert(1, "Drift since dry run")
view = st.radio(
    "Inspect",
    views,
    horizontal=True,
    key="inspect",
    label_visibility="collapsed",
)
viewing_revalidation = False
with st.container(border=True):
    if view == "Change set":
        sources: dict[SandboxObject, str] = {"diff": "Developer dry run"}
        if request["REVAL_SNAPSHOT_NS"]:
            sources["reval_diff"] = "Admin re-validation (current data)"
        which = st.radio(
            "Change set from",
            list(sources),
            index=len(sources) - 1,
            format_func=sources.__getitem__,
            horizontal=True,
            key=f"evidence_{rid}_{request.revision}",
        )
        available = ui.diff_browser(repo, request, which=which, key=f"admin_{which}")
        viewing_revalidation = which == "reval_diff" and available
    elif view == "Drift since dry run":
        drift = ui.drift_panel(repo, request, cfg, key="admin")
        if drift.get("total"):
            next_step = (
                "Return to review, then re-validate on current data."
                if request.status == Status.APPROVED
                else "Re-validate on current data before approving."
            )
            st.warning(
                f"The source or a lookup table changed after the sandbox run. {next_step}",
                icon=":material/sync_problem:",
            )
    else:
        ui.audit_trail(repo.audit(request.request_id))

if request["REVAL_SNAPSHOT_NS"]:
    same = bool(request["REVAL_MATCHES"])
    message = (
        "Re-validation on current data produced the **same** change set as the dry run."
        if same
        else "Re-validation on current data produced a **different** change set."
    )
    (st.success if same else st.warning)(message, icon=":material/compare_arrows:")
    ui.count_metrics(request.counts("REVAL_COUNTS"), row_count, label="Re-validated: ")

st.subheader("Decision", anchor=False)

if request.status == Status.SUBMITTED:
    if st.button(
        "Re-validate on current data",
        icon=":material/refresh:",
        disabled=own_request,
        help="Clone the table as it is now, run the same SQL and compare the change sets.",
    ):
        with st.spinner("Cloning the current table and re-running the SQL..."):
            if ui.call_api(
                repo, "revalidate", success="Re-validated", refresh_on_error=True, request_id=rid
            ):
                st.rerun()
    comment = st.text_area("Comment (required)", key=f"review_comment_{rid}", disabled=own_request)
    tolerance = st.number_input(
        "Allowed deviation in rows affected at execution (%)",
        min_value=0.0,
        max_value=100.0,
        value=0.0,
        step=1.0,
        key=f"tolerance_{rid}",
        disabled=own_request,
        help="Applies to approval. 0 means execution must affect exactly the approved rows; "
        "otherwise it rolls back.",
    )

    def _decide(decision: str) -> None:
        if not comment.strip():
            st.error("Please add a comment explaining your decision.", icon=":material/edit:")
        elif ui.call_api(
            repo,
            "review",
            success="Decision recorded",
            request_id=rid,
            decision=decision,
            comment=comment,
            tolerance_pct=tolerance,
            expected_revision=request.revision,
        ):
            st.rerun()

    if not request.run_is_current:
        st.warning("The latest sandbox run has not completed. Re-validate before approving.")
    needs_revalidation_view = bool(request["REVAL_SNAPSHOT_NS"]) and not (
        request["REVAL_MATCHES"] or viewing_revalidation
    )
    if needs_revalidation_view:
        st.info("Display the affected rows in the admin re-validation change set before approving.")
    can_approve = request.run_is_current and not own_request and not needs_revalidation_view
    if (
        st.button(
            "Approve",
            type="primary",
            icon=":material/check:",
            key=f"approve_{rid}_{request.revision}",
            disabled=not can_approve,
        )
        and can_approve
    ):
        _decide("APPROVE")
    if ui.confirm_button(
        "Reject",
        key=f"reject_{rid}_{request.revision}",
        icon=":material/close:",
        prompt="Reject this request? Rejection is final; the developer would have to start over.",
        confirm_label="Yes, reject",
        disabled=own_request,
    ):
        _decide("REJECT")
elif request.status == Status.APPROVED:
    st.info(
        f"Approved by **{ui.safe_md(request['REVIEWED_BY'])}**: "
        f"{ui.safe_md(request['REVIEW_COMMENT'])}",
        icon=":material/verified:",
    )
    st.caption(
        "Execution re-checks drift, verifies the SQL hash, runs the statement in a transaction "
        "and rolls back if the rows affected deviate from the approval."
    )
    expected = request.counts("REVAL_COUNTS") or request.counts("DRY_RUN_COUNTS")
    if ui.confirm_button(
        f"Execute on {cfg.source}",
        key=f"execute_{rid}_{request.revision}",
        icon=":material/bolt:",
        primary=True,
        prompt=(
            f"This changes the real table **{cfg.source}**: about "
            f"{expected.get('total', 0)} rows (tolerance {request['TOLERANCE_PCT'] or 0}%). "
            "It cannot be undone from this app."
        ),
        confirm_label="Yes, execute now",
        disabled=own_request,
    ):
        with st.spinner("Executing on the real table..."):
            done = ui.call_api(
                repo,
                "execute",
                success="Executed",
                refresh_on_error=True,
                request_id=rid,
                expected_revision=request.revision,
            )
        if done:
            st.rerun()
    reason = st.text_input(
        "Reason to return to review",
        key=f"send_back_reason_{rid}_{request.revision}",
        disabled=own_request,
    )
    if st.button(
        "Return to review",
        icon=":material/undo:",
        key=f"send_back_{rid}_{request.revision}",
        disabled=own_request or not reason.strip(),
    ) and ui.call_api(
        repo,
        "send_back",
        success="Returned to review",
        request_id=rid,
        comment=reason,
        expected_revision=request.revision,
    ):
        st.rerun()
elif request.status == Status.EXECUTING:
    st.info("Execution is in progress. Refresh in a moment.", icon=":material/sync:")
elif request.status in (Status.REJECTED, Status.CANCELLED, Status.FAILED):
    rejected = request.status == Status.REJECTED
    reason = request["REVIEW_COMMENT"] if rejected else request["LAST_ERROR"]
    st.info(
        f"This request is {request.status.lower()}. {ui.safe_md(reason or '')}",
        icon=":material/info:",
    )
    if request.counts("EXEC_COUNTS"):
        ui.count_metrics(request.counts("EXEC_COUNTS"), row_count, label="Rolled back: ")
elif request.counts("EXEC_COUNTS"):
    ui.count_metrics(request.counts("EXEC_COUNTS"), row_count, label="Executed: ")
if request["EXEC_QUERY_ID"]:
    st.caption(
        f"Execution query `{request['EXEC_QUERY_ID']}` took {request['EXEC_ELAPSED_MS']} ms."
    )
