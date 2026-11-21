import streamlit as st

from auditable_crud import ui
from auditable_crud.state import EDITABLE_STATUSES, TERMINAL_STATUSES, Status

repo = ui.get_repository()
actor = ui.current_actor(repo)

requests = repo.requests(requested_by=actor)
if requests.empty:
    st.info("You have no change requests yet.", icon=":material/inbox:")
    st.page_link("app_pages/choose_table.py", label="Choose a table", icon=":material/table_view:")
    st.stop()

with st.container(horizontal=True):
    for status in (
        Status.SUBMITTED,
        Status.APPROVED,
        Status.EXECUTED,
        Status.REJECTED,
        Status.FAILED,
    ):
        st.metric(ui.status_label(status), int((requests["STATUS"] == status).sum()), border=True)

all_statuses = [str(s) for s in Status]
active = [s for s in all_statuses if Status(s) not in TERMINAL_STATUSES]
shown = st.multiselect(
    "Status", all_statuses, default=active, format_func=ui.status_label, key="my_status"
)
listed = requests[requests["STATUS"].isin(shown)] if shown else requests
if listed.empty:
    st.caption("No requests with these statuses.")
    st.stop()

st.caption("Select a request to see its details.")
selected = ui.selected_request_id(listed, key="my_requests")
if selected is None:
    st.stop()

request, cfg = ui.load_request_by_id(repo, selected)
with st.container(horizontal=True, vertical_alignment="center"):
    ui.request_header(request)
    if (
        cfg
        and request.status in EDITABLE_STATUSES
        and st.button("Open in workbench", icon=":material/edit:")
    ):
        st.session_state.request_id = request.request_id
        st.switch_page("app_pages/workbench.py")

st.code(str(request["CANONICAL_SQL"] or request["SQL_TEXT"] or ""), language="sql", wrap_lines=True)
if request["REVIEW_COMMENT"]:
    reviewer = ui.safe_md(request["REVIEWED_BY"])
    comment = ui.safe_md(request["REVIEW_COMMENT"])
    if request.status == Status.REJECTED:
        st.error(f"**Rejected by {reviewer}:** {comment}", icon=":material/block:")
    elif request.status == Status.SUBMITTED:
        st.info(f"Previous review by **{reviewer}**. {comment}", icon=":material/history:")
    else:
        st.success(f"**Approved by {reviewer}:** {comment}", icon=":material/verified:")
audit = repo.audit(request.request_id)
ui.stage_counts(request, audit)

if cfg is None:
    st.caption(
        "This table is no longer managed. The request and its audit trail are still available."
    )
if (
    (
        request.status in (Status.SUBMITTED, Status.APPROVED)
        or (cfg is None and request.status in EDITABLE_STATUSES)
    )
    and ui.confirm_button(
        "Withdraw request",
        key=f"withdraw_{request.request_id}",
        icon=":material/undo:",
        prompt="Withdraw this request? To propose this change again, open a new request.",
        confirm_label="Yes, withdraw",
    )
    and ui.call_api(repo, "cancel", success="Withdrawn", request_id=request.request_id)
):
    st.rerun()

st.subheader("Audit trail", anchor=False)
ui.audit_trail(audit)
