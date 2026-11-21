-- Schemas inside the app database (the database itself is created by bootstrap/).

DEFINE SCHEMA {{ app_db }}.CORE
    COMMENT = 'Auditable CRUD workflow state, audit log and app procedures';

DEFINE SCHEMA {{ app_db }}.SANDBOX
    COMMENT = 'Per-change-request clones, streams and diff tables (dropped by the cleanup task)';

DEFINE SCHEMA {{ app_db }}.APPS
    COMMENT = 'Developer and admin Streamlit apps';

DEFINE SCHEMA {{ app_db }}.DEMO
    COMMENT = 'Sample configuration tables managed through Auditable CRUD';
