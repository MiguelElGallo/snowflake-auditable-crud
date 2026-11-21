import pandas as pd
import streamlit as st

from auditable_crud import ui
from auditable_crud.state import Status

repo = ui.get_repository()
actor = ui.current_actor(repo)
OPEN = [Status.SUBMITTED, Status.APPROVED]

tables = ui.table_lookup(repo)
st.caption(
    "Requests waiting for review or execution. You cannot review requests you submitted yourself."
)
chosen = st.multiselect("Tables", list(tables), key="queue_tables", placeholder="All tables")

queue = repo.requests(statuses=OPEN)
if chosen:
    queue = queue[queue["TABLE_ID"].isin(chosen)]
    st.dataframe(
        [
            {
                "Table": table_id,
                "Rows": repo.table_stats(tables[table_id]).get("ROW_COUNT"),
                "Business key": ", ".join(tables[table_id].key_columns),
                "Open requests": int((queue["TABLE_ID"] == table_id).sum()),
            }
            for table_id in chosen
        ],
        hide_index=True,
    )
own = queue["REQUESTED_BY"].str.upper() == actor.upper()

with st.container(horizontal=True):
    st.metric("Waiting for review", int((queue["STATUS"] == Status.SUBMITTED).sum()), border=True)
    st.metric(
        "Approved, not executed", int((queue["STATUS"] == Status.APPROVED).sum()), border=True
    )
    st.metric("Mine (cannot review)", int(own.sum()), border=True)

if queue.empty:
    where = " for the selected tables" if chosen else ""
    st.success(f"All caught up. No requests are waiting{where}.", icon=":material/done_all:")
    st.stop()

# Requests you can act on first; your own last and marked.
queue = queue.assign(YOURS=own).sort_values(
    ["YOURS", "UPDATED_AT"] if "UPDATED_AT" in queue else ["YOURS"]
)
if "UPDATED_AT" in queue:
    waiting = pd.Timestamp.now(tz="UTC") - pd.to_datetime(queue["UPDATED_AT"], utc=True)
    queue = queue.assign(WAITING_H=(waiting.dt.total_seconds() // 3600).astype("Int64"))
st.caption("Select a request to review it.")
selected = ui.selected_request_id(queue, key="queue")
if selected is not None:
    st.session_state.review_id = selected
    st.switch_page("app_pages/review.py")
