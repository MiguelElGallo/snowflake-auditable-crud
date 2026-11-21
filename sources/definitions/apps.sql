-- The compute pool runs the app servers. Each app uses its owner-rights
-- connection to execute SQL on the query warehouse.

DEFINE STREAMLIT {{ app_db }}.APPS.DEVELOPER_APP
    FROM 'asset://developer_app/'
    MAIN_FILE = 'streamlit_app.py'
    QUERY_WAREHOUSE = {{ warehouse }}
    RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'
    COMPUTE_POOL = {{ compute_pool }}
    TITLE = 'Auditable CRUD - propose a change'
    COMMENT = 'Clone, dry-run and submit DML change requests';

DEFINE STREAMLIT {{ app_db }}.APPS.ADMIN_APP
    FROM 'asset://admin_app/'
    MAIN_FILE = 'streamlit_app.py'
    QUERY_WAREHOUSE = {{ warehouse }}
    RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'
    COMPUTE_POOL = {{ compute_pool }}
    TITLE = 'Auditable CRUD - review and execute'
    COMMENT = 'Check drift, approve or reject, and execute DML change requests';
