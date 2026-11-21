# Change request reference

A request targets one managed table. Its ID ties together the developer's SQL, sandbox results, review decisions and source execution. The two apps use the following pages.

| App | Page | What it shows or does |
| --- | --- | --- |
| Developer | **Choose table** | Managed tables, business keys and per-request row limits. **Propose a change** creates a request. |
| Developer | **Change workbench** | SQL editor, optional Cortex draft, clone preview, changed rows and submission. |
| Developer | **My requests** | The signed-in user's requests and audit trail. Open an editable draft or withdraw a submitted or approved request. |
| Admin | **Review queue** | Submitted requests awaiting review and approved requests awaiting execution. |
| Admin | **Review request** | Submitted SQL, changes, drift, decisions and execution controls. |
| Admin | **History and audit** | Requests across tables. Filter by table, status or requester, then select a row for stage counts, query IDs and events. |

## Stored statuses

| `STATUS` | Meaning | Available next action |
| --- | --- | --- |
| `DRAFT` | A request exists. | Requester edits, dry-runs or discards it. |
| `DRY_RUN_OK` | A dry run succeeded. | Requester edits and reruns, submits the latest successful run, or discards it. |
| `SUBMITTED` | The requester submitted the SQL and its preview. | Another user with Admin access revalidates, approves or rejects it. The requester can withdraw. |
| `APPROVED` | A reviewer recorded a comment, the SQL hash and the allowed deviation. | A non-requesting admin can execute or return it to review. The requester can withdraw. |
| `EXECUTING` | The source transaction has claimed the request. | The transaction completes or rolls back. |
| `EXECUTED` | Snowflake committed the source DML and success event. | Inspect history. A correction requires a new request. |
| `FAILED` | Execution failed. The transaction rolled back. | Inspect the error and any recorded counts or query ID. Open a new request for a corrected change. |
| `REJECTED` | A reviewer declined the request and recorded a comment. | Inspect the decision. The requester can propose a new change. |
| `CANCELLED` | The requester discarded or withdrew the request, or configured cleanup expired an idle draft. | Inspect history. The requester can propose a new change. |

The approving reviewer can also execute the request. The requester cannot revalidate, approve, reject or execute it, including when their account has both app roles. A stale review screen must reload the current revision before a decision can succeed.

## SQL accepted by the editor

| Input | Rule |
| --- | --- |
| Statement | One Snowflake `INSERT`, `UPDATE`, `DELETE` or `MERGE` against the selected table. |
| Table reads | The target and its configured lookup tables. Unqualified names resolve in the target's database and schema. The workflow manages the snapshot for historical reads. |
| Expressions | The validator accepts a defined set of built-in functions. User-defined functions, table functions, sequence references, random sampling and row limits are rejected. |
| Row selection | Use deterministic keys or aggregates and fixed dates in predicates. An `UPDATE … FROM` must have at most one matching source row per target row. |
| Clock values | A direct assignment such as `SET updated_at = CURRENT_TIMESTAMP()` takes its value at each run, including source execution. |
| Target defaults | A table with a sequence-backed column default is ineligible because a clone can share the source sequence. |
| Cortex | **Ask Cortex → Generate SQL** sends the target name, column definitions, business keys, up to five sample rows and the user's prompt to Cortex. The statement appears in the editor and follows the usual preview and review flow. |

The [SQL validator](../../streamlit/common/auditable_crud/sql_guard.py) defines the complete expression rules. The [statement settings](../../streamlit/common/auditable_crud/runner.py) apply to every dry run, revalidation and source execution. They use UTC, `TIMESTAMP_NTZ` as the default timestamp type, `YYYY-MM-DD` for date input, and `AUTO` for time and timestamp input. `WEEK_START` and `WEEK_OF_YEAR_POLICY` are both `0`. `DAYOFWEEKISO`, `WEEKISO` and `YEAROFWEEKISO` express ISO calendar semantics explicitly. Binary, JSON and date/time output formats are fixed as well.

## Limits and evidence

Submission requires the same SQL as the latest completed dry run and between one row and the table's `max_affected_rows`, inclusive. Approval requires a comment and current evidence. The approver sets a tolerance of `0` to `100` percent. At execution the tolerance applies separately to inserted, updated and deleted row counts. If the preview expects zero inserts, any insert exceeds its allowance. The table's maximum always applies to the total.

| Evidence | Where to look |
| --- | --- |
| Before/after values, changed columns, inserted and deleted rows | **Change set** in the review page, or **Dry-run result** in the workbench. **Download this page as CSV** exports the displayed page. |
| Counts, elapsed time and query ID for each recorded stage | **History and audit → Request details**. A rolled-back attempt can have a query ID. Check the final status. |
| SQL reporting | `CORE.V_REQUESTS` exposes status, actors and stage totals. `CORE.V_AUDIT` exposes timestamped events, actors and details. `AC_ADMIN` has read access to both views. |
| Evidence after sandbox cleanup | The row-level change set is removed. The request, counts and audit events remain. |

To see how these steps fit together, test and approve a sample configuration change or follow a request that needs another review. [Configuration table tutorial](../tutorials/first-change.md) · [Why a request can return to review](../explanation/approval-and-drift.md)
