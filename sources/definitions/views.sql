-- Managed-table configuration, rendered from manifest.yml `managed_tables`, so
-- adding a table is a reviewed, versioned DCM deployment.
{% macro sql_str(value) %}'{{ value | string | replace('\\', '\\\\') | replace("'", "''") }}'{% endmacro %}

DEFINE VIEW {{ app_db }}.CORE.V_MANAGED_TABLES
    COMMENT = 'Tables that may be changed through Auditable CRUD (from manifest.yml)'
AS
SELECT
    COLUMN1::VARCHAR AS TABLE_ID,
    COLUMN2::VARCHAR AS SOURCE_DATABASE,
    COLUMN3::VARCHAR AS SOURCE_SCHEMA,
    COLUMN4::VARCHAR AS SOURCE_TABLE,
    COLUMN5::VARCHAR AS CLONE_DATABASE,
    COLUMN6::VARCHAR AS CLONE_SCHEMA,
    COLUMN7::VARCHAR AS KEY_COLUMNS,
    COLUMN8::VARCHAR AS ALLOWED_READ_TABLES,
    COLUMN9::NUMBER AS MAX_AFFECTED_ROWS,
    COLUMN10::VARCHAR AS DESCRIPTION,
    COLUMN11::VARCHAR AS AI_MODEL
FROM VALUES
{% for t in managed_tables %}
    (
        {{ sql_str(t.table_id | trim | upper) }},
        {{ sql_str(t.source_database) }},
        {{ sql_str(t.source_schema) }},
        {{ sql_str(t.source_table) }},
        {{ sql_str(t.clone_database | default(app_db)) }},
        {{ sql_str(t.clone_schema | default('SANDBOX')) }},
        {{ sql_str(t.key_columns) }},
        {{ sql_str(t.allowed_read_tables | default('')) }},
        {{ t.max_affected_rows | default(10000) | int }},
        {{ sql_str(t.description | default('')) }},
        {{ sql_str(t.ai_model | default('claude-sonnet-4-5')) }}
    ){{ "," if not loop.last }}
{% else %}
    (NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
WHERE FALSE
{% endfor %};

DEFINE VIEW {{ app_db }}.CORE.V_REQUESTS
    COMMENT = 'Change requests with flattened statistics, for reporting'
AS
SELECT
    REQUEST_ID,
    TABLE_ID,
    SOURCE_FQN,
    STATUS,
    STATEMENT_KIND,
    REQUESTED_BY,
    REVIEWED_BY,
    EXECUTED_BY,
    DRY_RUN_COUNTS:total::NUMBER AS DRY_RUN_ROWS,
    REVAL_COUNTS:total::NUMBER AS REVALIDATED_ROWS,
    EXEC_COUNTS:total::NUMBER AS EXECUTED_ROWS,
    DRY_RUN_ELAPSED_MS,
    EXEC_ELAPSED_MS,
    REVAL_MATCHES,
    CREATED_AT,
    SUBMITTED_AT,
    REVIEWED_AT,
    EXECUTED_AT,
    DATEDIFF('minute', SUBMITTED_AT, COALESCE(EXECUTED_AT, REVIEWED_AT)) AS REVIEW_MINUTES
FROM {{ app_db }}.CORE.CHANGE_REQUESTS;

DEFINE VIEW {{ app_db }}.CORE.V_AUDIT
    COMMENT = 'Audit trail joined with request context'
AS
SELECT a.EVENT_ID, a.EVENT_AT, a.EVENT, a.ACTOR, a.REQUEST_ID, r.TABLE_ID, r.STATUS, a.DETAILS
FROM {{ app_db }}.CORE.AUDIT_LOG a
LEFT JOIN {{ app_db }}.CORE.CHANGE_REQUESTS r ON r.REQUEST_ID = a.REQUEST_ID;
