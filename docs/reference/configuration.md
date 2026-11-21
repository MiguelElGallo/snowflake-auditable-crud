# Configuration reference

DCM reads `manifest.yml` at the project root. The repository ships a [template](../../manifest.example.yml). A deployment’s local copy supplies its account, roles and managed tables. The template includes a `DEV` target and uses database `AUDITABLE_CRUD`.

## Target, compute and roles

| Field | Value in a new installation |
| --- | --- |
| `targets.DEV.account_identifier` | Required. Replace the placeholder with the Snowflake organization and account identifier for the deployment. |
| `targets.DEV.project_name` | `AUDITABLE_CRUD.DCM.AUDITABLE_CRUD_PROJECT`. The bootstrap creates this object. |
| `targets.DEV.project_owner` | `AC_OWNER`, the account role that owns the workflow and both apps. |
| `templating.defaults.app_db` | `AUDITABLE_CRUD`. The supplied bootstrap, seed and package-attachment commands use this database name. |
| `templating.defaults.compute_pool` | `SYSTEM_COMPUTE_POOL_CPU`. Runs the Python 3.11 Streamlit servers. |
| `templating.defaults.warehouse` | `COMPUTE_WH`. Runs SQL and the scheduled cleanup task. |
| `templating.configurations.DEV.developer_account_roles` | A list of existing account roles that receive `AUDITABLE_CRUD.AC_DEVELOPER` and can open the Developer app. Replace the placeholder role. |
| `templating.configurations.DEV.admin_account_roles` | A list of existing account roles that receive `AUDITABLE_CRUD.AC_ADMIN`, can open the Admin app and can query `CORE.V_REQUESTS` and `CORE.V_AUDIT`. Replace the placeholder role. |
| `templating.defaults.cleanup_retention_days` | `7` days. A terminal request’s sandbox becomes eligible for removal after this interval. |
| `templating.defaults.cleanup_idle_draft_days` | `0` keeps idle drafts. A positive number permits expiry of idle editable requests. |

The role lists default to empty outside the `DEV` configuration. An empty list grants that database role to no account role through the manifest. DCM can remove grants when you remove entries. Reserve `AC_OWNER` and direct procedure access for trusted operators. The apps pass the signed-in viewer to the procedures. [How viewer identity works](../explanation/approval-and-drift.md)

## Managed tables

Each entry in `templating.defaults.managed_tables` becomes a row in `CORE.V_MANAGED_TABLES`. Use plain Snowflake identifiers. An empty list produces an empty table chooser.

| Field | Type and behavior |
| --- | --- |
| `table_id` | Required string. Unique after trimming whitespace and folding to uppercase. Appears in the table chooser. |
| `source_database`, `source_schema`, `source_table` | Required strings. The three identifiers of the table to change. |
| `key_columns` | Required string. Comma-separated business key columns used to check for overlapping changes. At least one column is required. |
| `clone_database`, `clone_schema` | Optional strings. Default to `app_db` and `SANDBOX`. Hold per-request clones, streams and row differences. |
| `allowed_read_tables` | Optional string. Comma-separated, fully qualified lookup table names. The default empty string permits no additional tables. |
| `max_affected_rows` | Positive integer. Defaults to `10000`. The example limits `FEATURE_FLAGS` to `200` and `PRICING_CONFIG` to `1000`. |
| `description` | Optional string shown in the table chooser. Defaults to an empty string. |
| `ai_model` | Optional model identifier for Cortex SQL drafting. Defaults to `claude-sonnet-4-5`. Model availability depends on the account and region. |

## Objects you will see

| Schema | Objects and purpose |
| --- | --- |
| `APPS` | `DEVELOPER_APP` and `ADMIN_APP`. Each has its own database-role grant. |
| `CORE` | `SP_DEV_API` and `SP_ADMIN_API`, `CHANGE_REQUESTS`, `AUDIT_LOG`, the managed-table and reporting views, and `TSK_CLEANUP_SANDBOXES`. |
| `SANDBOX` | Per-request working copies, change streams and stored row differences. |
| `DEMO` | `FEATURE_FLAGS`, `PRICING_CONFIG` and the read-only pricing lookup `REGIONS`. |
| `DCM` | The DCM project created by the bootstrap. |

The field definitions are in the [managed-table view](../../sources/definitions/views.sql) and [access grants](../../sources/definitions/access.sql).
