# Onboard a table

Use this when you want developers to propose changes to an existing Snowflake table. You need a role that can enable change tracking on the table and grant the project owner access. The project expects a local primary table with Time Travel. The sample uses an internal `PRODUCT_CATALOG.CONFIG.DISPLAY_SETTINGS` table with a unique `SETTING_KEY` column.

1. Identify columns whose values reliably identify one business row. For a flag defined per environment, use both the flag key and environment. Check that the key is unique in the source. The workflow uses these values to report which changed rows overlap a request.
2. Set the table’s Time Travel retention to cover your review window. The onboarding script requires at least one day. Avoid sequence-backed column defaults in managed targets. A clone can share a sequence with its source. [Snowflake cloning and sequences](https://docs.snowflake.com/en/user-guide/object-clone#cloning-and-default-sequences)
3. Make a local copy of the onboarding script. Set `src_db`, `src_schema` and `src_table`. Leave `is_managed_target` set to `TRUE` for the table you will update. Run that copy as its owner or a role with the required privileges. The script enables change tracking, grants namespace usage, and grants `AC_OWNER` read and write access to the target. [Onboarding SQL](../../bootstrap/20_onboard_existing_table.sql)
4. For each table the proposed SQL needs to read, run the script for that table with `is_managed_target = FALSE`. This grants read access. Enable tracking before creating requests so that Snowflake can compare lookup changes with the original preview.
5. Add an entry under `templating.defaults.managed_tables` in your local `manifest.yml`. Keep the other managed-table entries. The example below allows up to 20 changed rows per request.

```yaml
- table_id: DISPLAY_SETTINGS
  source_database: PRODUCT_CATALOG
  source_schema: CONFIG
  source_table: DISPLAY_SETTINGS
  key_columns: SETTING_KEY
  max_affected_rows: 20
  description: Display settings used by the catalog
```

If you onboarded a lookup, add its three-part name to `allowed_read_tables`. For a custom sandbox, grant `AC_OWNER` usage on the destination database and schema and permission to create tables and streams there. [Managed-table settings](../reference/configuration.md)

Review and apply the DCM plan, then open **Choose table**. Select **Propose a change** on your new table. The workbench should identify the intended source and sandbox. You can discard this draft after checking the configuration. [Deployment steps](deploy.md#review-the-plan-and-apply-it)
