-- =============================================================================
-- One-time bootstrap for the Auditable CRUD DCM project.
-- Run as ACCOUNTADMIN:  snow sql -f bootstrap/00_admin_bootstrap.sql -c <conn>
--
-- Why this exists: a DCM project cannot define its own parent database/schema,
-- and the Streamlit apps use owner's rights for their SQL connections.
-- We therefore create a dedicated least-privilege owner role, AC_OWNER, that
-- owns the project, the procedures and the apps. ACCOUNTADMIN is only used here.
-- Idempotent: safe to re-run.
-- =============================================================================
USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS AC_OWNER
    COMMENT = 'Owns the Auditable CRUD DCM project, its procedures and Streamlit apps';
GRANT ROLE AC_OWNER TO ROLE SYSADMIN;

-- The deploying user (the user running this script) gets the owner role so
-- `snow dcm deploy --role AC_OWNER` works.
SET deploy_user = CURRENT_USER();
GRANT ROLE AC_OWNER TO USER IDENTIFIER($deploy_user);

-- A rerun must not take ownership of somebody else's same-named database.
EXECUTE IMMEDIATE $$
DECLARE
    existing_count INTEGER;
    existing_owner VARCHAR;
    unexpected_owner EXCEPTION (-20001, 'AUDITABLE_CRUD already exists with another owner. Verify the database before onboarding it.');
BEGIN
    SHOW DATABASES LIKE 'AUDITABLE_CRUD';
    SELECT COUNT(*), MAX("owner") INTO :existing_count, :existing_owner
        FROM TABLE(RESULT_SCAN(:SQLID)) WHERE "name" = 'AUDITABLE_CRUD';
    IF (existing_count = 0) THEN
        CREATE DATABASE AUDITABLE_CRUD COMMENT = 'Auditable CRUD: governed DML change requests';
        GRANT OWNERSHIP ON DATABASE AUDITABLE_CRUD TO ROLE AC_OWNER COPY CURRENT GRANTS;
    ELSEIF (existing_owner IS DISTINCT FROM 'AC_OWNER') THEN
        RAISE unexpected_owner;
    END IF;
END;
$$;

-- Query compute for the apps, the procedures and the cleanup task.
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE AC_OWNER;
-- The daily sandbox-cleanup task.
GRANT EXECUTE TASK ON ACCOUNT TO ROLE AC_OWNER;
-- AI_COMPLETE for "generate SQL from a prompt".
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE AC_OWNER;

USE ROLE AC_OWNER;
CREATE SCHEMA IF NOT EXISTS AUDITABLE_CRUD.DCM
    COMMENT = 'Holds the DCM project object only';
CREATE DCM PROJECT IF NOT EXISTS AUDITABLE_CRUD.DCM.AUDITABLE_CRUD_PROJECT
    COMMENT = 'Auditable CRUD: clone, dry-run, approve and execute DML';
