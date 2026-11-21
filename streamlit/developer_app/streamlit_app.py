import streamlit as st

from auditable_crud import ui
from auditable_crud.repository import DEVELOPER_API

st.set_page_config(page_title="Auditable CRUD - propose a change", page_icon=":material/edit_note:")

st.session_state.setdefault("table_id", None)
st.session_state.setdefault("request_id", None)
repo = ui.get_repository(DEVELOPER_API)

page = st.navigation(
    [
        st.Page("app_pages/choose_table.py", title="Choose table", icon=":material/table_view:"),
        st.Page("app_pages/workbench.py", title="Change workbench", icon=":material/science:"),
        st.Page("app_pages/my_requests.py", title="My requests", icon=":material/history:"),
    ],
    position="top",
)
st.title(page.title, anchor=False)
ui.signed_in_caption(repo)
page.run()
