from collections import Counter

import streamlit as st

from auditable_crud import ui
from auditable_crud.state import EDITABLE_STATUSES, TERMINAL_STATUSES, Status

repo = ui.get_repository()
actor = ui.current_actor(repo)

st.caption(
    "Pick the table you want to change: the app clones it into an isolated sandbox where your "
    "SQL runs first. Nothing touches the real table until an admin approves your request."
)

mine = repo.requests(requested_by=actor, statuses=[s for s in Status if s not in TERMINAL_STATUSES])
open_requests = Counter(str(table_id) for table_id in mine["TABLE_ID"])
editable = mine[mine["STATUS"].isin(EDITABLE_STATUSES)]
open_drafts = Counter(str(table_id) for table_id in editable["TABLE_ID"])

chosen = ui.table_picker(
    repo,
    button_label="Propose a change",
    key="pick",
    open_requests=open_requests,
    open_drafts=open_drafts,
)
if chosen is not None:
    with st.spinner(f"Cloning {chosen.source} into the sandbox..."):
        created = ui.call_api(repo, "create_request", table_id=chosen.table_id)
    if created:
        st.session_state.table_id = chosen.table_id
        st.session_state.request_id = created["request_id"]
        st.switch_page("app_pages/workbench.py")
    else:
        st.caption("Nothing was created. Fix the problem above and try again.")
