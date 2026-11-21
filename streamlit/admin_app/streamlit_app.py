import streamlit as st

from auditable_crud import ui
from auditable_crud.repository import ADMIN_API

st.set_page_config(page_title="Auditable CRUD - review", page_icon=":material/gavel:")

st.session_state.setdefault("review_id", None)
repo = ui.get_repository(ADMIN_API)

page = st.navigation(
    [
        st.Page("app_pages/queue.py", title="Review queue", icon=":material/inbox:"),
        st.Page("app_pages/review.py", title="Review request", icon=":material/fact_check:"),
        st.Page("app_pages/history.py", title="History and audit", icon=":material/history:"),
    ],
    position="top",
)
st.title(page.title, anchor=False)
ui.signed_in_caption(repo)
page.run()
