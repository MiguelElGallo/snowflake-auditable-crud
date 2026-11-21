# Inspect a run and manage retention

## Trace a request to its execution

Open **History and audit** in the Admin app. Filter by table, status or requester, then select the request. Review the stored statement, run counts and events. The final execution has its own Snowflake query ID.

An account role with the `AC_ADMIN` database role can also query the reporting views. Replace the example request ID with the one shown in the app.

```sql
SELECT EVENT_AT, EVENT, ACTOR, DETAILS
FROM AUDITABLE_CRUD.CORE.V_AUDIT
WHERE REQUEST_ID = 'your-request-id'
ORDER BY EVENT_AT, EVENT_ID;
```

To investigate runtime or bytes scanned, use an account role with access to that query in Snowflake query history, and replace the example query ID. The Information Schema function returns recent history, and the result limit applies before the outer filter. [Snowflake query history](https://docs.snowflake.com/en/sql-reference/functions/query_history)

```sql
SELECT QUERY_ID, BYTES_SCANNED, TOTAL_ELAPSED_TIME
FROM TABLE(AUDITABLE_CRUD.INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => 10000))
WHERE QUERY_ID = 'your-query-id';
```

## Remove expired working copies

The daily task starts suspended. Set `cleanup_retention_days` in your local manifest to the number of days you want to retain completed request sandboxes. `cleanup_idle_draft_days = 0` keeps editable drafts. A positive value allows idle drafts to expire. Plan and deploy any configuration change before enabling the schedule. [Deployment steps](deploy.md#review-the-plan-and-apply-it)

As the task owner, resume the task when the retention policy is ready.

```sql
ALTER TASK AUDITABLE_CRUD.CORE.TSK_CLEANUP_SANDBOXES RESUME;
SHOW TASKS LIKE 'TSK_CLEANUP_SANDBOXES' IN SCHEMA AUDITABLE_CRUD.CORE;
```

Check that the task is started. It runs at 03:00 UTC. A cleanup failure appears in task history. Individual sandbox removal errors also produce `SANDBOX_DROP_FAILED` events in `CORE.AUDIT_LOG`. The request and audit events remain available after the sandbox is removed. [Task definition](../../sources/definitions/jobs.sql)
