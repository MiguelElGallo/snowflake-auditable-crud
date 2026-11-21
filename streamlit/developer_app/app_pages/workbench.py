import streamlit as st

from auditable_crud import ui
from auditable_crud.sql_guard import GuardError, guard_dml
from auditable_crud.state import EDITABLE_STATUSES

SQL_KEY = "sql_editor"
PROMPT_KEY = "nl_prompt"

repo = ui.get_repository()
actor = ui.current_actor(repo)

if not st.session_state.get("request_id"):
    st.info("Choose a table first to open a change request.", icon=":material/arrow_back:")
    st.page_link("app_pages/choose_table.py", label="Choose table", icon=":material/table_view:")
    st.stop()

request, cfg = ui.load_request(
    repo, "request_id", "app_pages/choose_table.py", "Choose table", requested_by=actor
)
rid = request.request_id
editable = request.status in EDITABLE_STATUSES
draft_key = f"workbench_draft_{rid}"
note_key = f"submit_note_{rid}"
if not editable:
    st.session_state.pop(draft_key, None)
draft = st.session_state.setdefault(draft_key, {}) if editable else {}

# Streamlit removes widgets when their page closes. Keep edited values for
# this request in the viewer's session, separately from its published SQL.
if st.session_state.get("editor_for") != rid:
    for field in (SQL_KEY, PROMPT_KEY, "mode"):
        st.session_state.pop(field, None)
    st.session_state.editor_for = rid
for field in (SQL_KEY, PROMPT_KEY, "mode", note_key):
    if field in draft and field not in st.session_state:
        st.session_state[field] = draft[field]
saved_sql = str(request["SQL_TEXT"] or "")
if SQL_KEY not in st.session_state or not editable:
    st.session_state[SQL_KEY] = saved_sql


def _remember(field: str) -> None:
    draft[field] = st.session_state[field]


ui.request_header(request)
if not request["SANDBOX_DROPPED_AT"]:
    st.caption(f"Sandbox clone: `{request.sandbox('clone')}`")

if st.toggle(f"Show schema and sample of {cfg.source}", key=f"preview_{rid}"):
    columns = repo.columns(cfg)
    st.dataframe(
        {"column": [c for c, _ in columns], "type": [t for _, t in columns]},
        hide_index=True,
    )
    st.dataframe(ui.display_table_values(repo.preview(cfg, 50)), hide_index=True)


with st.container(border=True):
    mode = st.radio(
        "How do you want to write the change?",
        ["Write SQL", "Ask Cortex"],
        horizontal=True,
        key="mode",
        disabled=not editable,
        on_change=_remember,
        args=("mode",),
    )
    if mode == "Ask Cortex":
        prompt = st.text_area(
            "Describe the change",
            key=PROMPT_KEY,
            disabled=not editable,
            placeholder="e.g. Increase EU prices by 5% for active SKUs above 100 EUR",
            on_change=_remember,
            args=(PROMPT_KEY,),
        )
        can_generate = editable and bool(prompt.strip())
        if (
            st.button(
                "Generate SQL",
                icon=":material/auto_awesome:",
                key=f"generate_{rid}",
                disabled=not can_generate,
            )
            and can_generate
        ):
            try:
                candidate = repo.generate_sql(cfg, prompt)
                if not candidate:
                    raise ValueError("Cortex did not return a single DML statement.")
            except Exception as error:  # Cortex/Snowpark errors must not discard the draft
                st.error(
                    f"Cortex could not generate SQL: {ui.safe_md(error)}", icon=":material/error:"
                )
            else:
                st.session_state[SQL_KEY] = candidate
                draft.update(
                    {SQL_KEY: candidate, "generated_sql": candidate, "generated_from": prompt}
                )
        st.caption("Generated SQL lands in the editor below. Review it before the dry run.")
    sql_text = st.text_area(
        f"DML against {cfg.source}",
        key=SQL_KEY,
        height=180,
        disabled=not editable,
        placeholder=f"UPDATE {cfg.source.name} SET ... WHERE ...",
        on_change=_remember,
        args=(SQL_KEY,),
    )
    if sql_text == saved_sql:
        draft.pop(SQL_KEY, None)

    guard_error = None
    if sql_text.strip():
        try:
            guarded = guard_dml(sql_text, cfg.source, cfg.allowed_reads)
            st.success(f"Valid {guarded.kind} on {cfg.source}.", icon=":material/check:")
        except GuardError as error:
            guard_error = str(error)
            st.error(ui.safe_md(guard_error), icon=":material/block:")

    if st.button(
        "Dry run on the clone",
        type="primary",
        icon=":material/play_arrow:",
        disabled=not editable or not sql_text.strip() or guard_error is not None,
    ):
        nl_prompt = None
        if sql_text == draft.get("generated_sql"):
            nl_prompt = draft.get("generated_from")
        elif sql_text == request["SQL_TEXT"]:
            nl_prompt = request["NL_PROMPT"]
        with st.spinner("Re-cloning, running your SQL on the clone and capturing the stream..."):
            ran = ui.call_api(
                repo,
                "dry_run",
                success="Dry run finished",
                refresh_on_error=True,
                request_id=request.request_id,
                sql=sql_text,
                nl_prompt=nl_prompt,
            )
        if ran:
            st.rerun()

if (
    editable
    and ui.confirm_button(
        "Discard request",
        key=f"discard_{rid}",
        icon=":material/delete:",
        prompt="Discard this request? A discarded request cannot be reopened.",
        confirm_label="Yes, discard",
    )
    and ui.call_api(repo, "cancel", success="Cancelled", request_id=rid)
):
    st.session_state.pop(draft_key, None)
    st.session_state.request_id = None
    st.switch_page("app_pages/choose_table.py")

if not request.counts("DRY_RUN_COUNTS"):
    st.stop()

st.subheader("Dry-run result", anchor=False)
counts = request.counts("DRY_RUN_COUNTS")
row_count = ui.table_stats_rows(repo, cfg)
ui.count_metrics(counts, row_count)
st.caption(
    f"{request['STATEMENT_KIND']} ran in **{request['DRY_RUN_ELAPSED_MS']} ms** "
    f"(query `{request['DRY_RUN_QUERY_ID']}`). The stream on the clone recorded "
    f"{request.counts('DRY_RUN_DIFF_COUNTS').get('total', 0)} changed rows."
)
with st.container(border=True):
    ui.diff_browser(repo, request, which="diff", key="dev_diff")

if editable:
    total = counts.get("total", 0)
    blocked = None
    if not request.run_is_current:
        blocked = "The latest sandbox run has not completed. Run it again before submitting."
    elif sql_text.strip() != str(request["SQL_TEXT"] or "").strip():
        blocked = (
            "The SQL in the editor differs from the one that was dry-run, so the result above "
            "is out of date. Run the dry run again before submitting."
        )
    elif total == 0:
        blocked = "The statement changed no rows, so there is nothing to submit."
    elif total > cfg.max_affected_rows:
        blocked = f"{total} rows exceed the limit of {cfg.max_affected_rows} for this table."
    with st.container(border=True):
        st.markdown("**Happy with the result?** Send it to an admin for approval.")
        if blocked:
            st.warning(blocked, icon=":material/warning:")
        note = st.text_input(
            "Note for the reviewer (optional)", key=note_key, on_change=_remember, args=(note_key,)
        )
        with st.container(horizontal=True):
            if st.button(
                "Submit for approval",
                type="primary",
                icon=":material/send:",
                key=f"submit_{rid}_{request.revision}",
                disabled=blocked is not None,
            ) and ui.call_api(
                repo,
                "submit",
                success="Submitted",
                request_id=rid,
                note=note,
                sql=sql_text,
                expected_revision=request.revision,
            ):
                st.session_state.pop(draft_key, None)
                st.rerun()
else:
    st.info(
        f"This request is {request.status.replace('_', ' ').lower()}; it can no longer be edited.",
        icon=":material/lock:",
    )
    st.page_link(
        "app_pages/my_requests.py", label="Follow it under My requests", icon=":material/list:"
    )
