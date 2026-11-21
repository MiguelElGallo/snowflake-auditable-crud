import streamlit as st

from auditable_crud import ui
from auditable_crud.state import Status

repo = ui.get_repository()
ui.current_actor(repo)

history = repo.requests()
if history.empty:
    st.info("No change requests yet.", icon=":material/inbox:")
    st.stop()

with st.container(horizontal=True):
    st.metric("Requests", len(history), border=True)
    for status in (Status.EXECUTED, Status.REJECTED, Status.FAILED, Status.CANCELLED):
        st.metric(ui.status_label(status), int((history["STATUS"] == status).sum()), border=True)

by_status = history.groupby(["TABLE_ID", "STATUS"]).size().reset_index(name="REQUESTS")
statuses = sorted(by_status["STATUS"].unique())
st.bar_chart(
    by_status,
    x="TABLE_ID",
    y="REQUESTS",
    color="STATUS",
    horizontal=True,
    x_label="Requests",
    y_label="Table",
)

with st.container(horizontal=True):
    tables = st.multiselect(
        "Table", sorted(history["TABLE_ID"].unique()), key="hist_tables", placeholder="All"
    )
    status_filter = st.multiselect(
        "Status", statuses, format_func=ui.status_label, key="hist_status", placeholder="All"
    )
    who = st.text_input("Requested by", key="hist_user", placeholder="Any user")
filtered = history
if tables:
    filtered = filtered[filtered["TABLE_ID"].isin(tables)]
if status_filter:
    filtered = filtered[filtered["STATUS"].isin(status_filter)]
if who.strip():
    filtered = filtered[filtered["REQUESTED_BY"].str.contains(who.strip(), case=False, regex=False)]
st.caption(f"{len(filtered)} of {len(history)} requests. Select one to see its details.")

request_id = ui.selected_request_id(filtered, key="history")
if request_id is not None:
    request, cfg = ui.load_request_by_id(repo, request_id)
    st.subheader("Request details", anchor=False)
    ui.request_header(request)
    if cfg is None:
        st.caption("This table is no longer managed. Its audit trail is still available.")
    if (
        cfg is not None
        and request.status in (Status.SUBMITTED, Status.APPROVED)
        and st.button("Open in review", icon=":material/fact_check:")
    ):
        st.session_state.review_id = request_id
        st.switch_page("app_pages/review.py")
    st.code(
        str(request["CANONICAL_SQL"] or request["SQL_TEXT"] or ""), language="sql", wrap_lines=True
    )
    audit = repo.audit(request_id)
    ui.stage_counts(request, audit)
    st.subheader("Audit trail", anchor=False)
    ui.audit_trail(audit)
