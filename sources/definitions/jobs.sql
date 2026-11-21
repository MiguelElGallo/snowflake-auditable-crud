-- Daily removal of sandbox objects (clones, streams, diffs) of final requests.
-- New tasks deploy SUSPENDED; resume with:
--   ALTER TASK {{ app_db }}.CORE.TSK_CLEANUP_SANDBOXES RESUME;

DEFINE TASK {{ app_db }}.CORE.TSK_CLEANUP_SANDBOXES
    WAREHOUSE = '{{ warehouse }}'
    SCHEDULE = 'USING CRON 0 3 * * * UTC'
    COMMENT = 'Drop sandboxes of final change requests older than the retention period'
AS
DECLARE
    cleanup_response VARIANT;
    procedure_failed EXCEPTION (-20001, 'The cleanup procedure returned an error. Check the procedure call in query history.');
    sandbox_failed EXCEPTION (-20002, 'Sandbox cleanup was incomplete. Check CORE.AUDIT_LOG for SANDBOX_DROP_FAILED events.');
BEGIN
    CALL {{ app_db }}.CORE.SP_ADMIN_API(
        'cleanup',
        OBJECT_CONSTRUCT(
            'actor', 'SYSTEM:TSK_CLEANUP_SANDBOXES',
            'retention_days', {{ cleanup_retention_days | int }},
            'idle_draft_days', {{ cleanup_idle_draft_days | int }}
        )
    ) INTO :cleanup_response;

    IF (AS_BOOLEAN(GET(cleanup_response, 'ok')) IS DISTINCT FROM TRUE) THEN
        RAISE procedure_failed;
    END IF;
    IF (AS_INTEGER(GET(GET(cleanup_response, 'result'), 'failed')) IS DISTINCT FROM 0) THEN
        RAISE sandbox_failed;
    END IF;
END;
