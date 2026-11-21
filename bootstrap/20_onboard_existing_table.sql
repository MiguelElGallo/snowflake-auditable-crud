-- =============================================================================
-- Onboard an EXISTING table (one that this DCM project does not define) so it
-- can be managed by Auditable CRUD. Run as the table owner (or ACCOUNTADMIN),
-- then add the table to `managed_tables` in manifest.yml and redeploy.
--
-- Replace the identifiers below. Set is_managed_target to FALSE when onboarding
-- a read-only lookup from allowed_read_tables.
-- =============================================================================
SET src_db = 'MY_DB';
SET src_schema = 'MY_SCHEMA';
SET src_table = 'MY_TABLE';
SET is_managed_target = TRUE;
SET src_fqn = $src_db || '.' || $src_schema || '.' || $src_table;
SET src_schema_fqn = $src_db || '.' || $src_schema;

EXECUTE IMMEDIATE $$
DECLARE
    days INTEGER;
    insufficient_retention EXCEPTION (-20002, 'The table needs at least one day of Time Travel. Set a retention period that covers your review window and rerun.');
BEGIN
    SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN TABLE IDENTIFIER($src_fqn);
    SELECT "value"::INTEGER INTO :days FROM TABLE(RESULT_SCAN(:SQLID))
        WHERE "key" = 'DATA_RETENTION_TIME_IN_DAYS';
    IF (days IS NULL OR days < 1) THEN
        RAISE insufficient_retention;
    END IF;
END;
$$;

-- Drift detection reads the source with CHANGES(...) AT(TIMESTAMP => snapshot):
-- change tracking must be on BEFORE any change request is created.
ALTER TABLE IDENTIFIER($src_fqn) SET CHANGE_TRACKING = TRUE;

-- AC_OWNER runs the clone, the drift checks and the approved DML.
GRANT USAGE ON DATABASE IDENTIFIER($src_db) TO ROLE AC_OWNER;
GRANT USAGE ON SCHEMA IDENTIFIER($src_schema_fqn) TO ROLE AC_OWNER;
GRANT SELECT ON TABLE IDENTIFIER($src_fqn) TO ROLE AC_OWNER;
EXECUTE IMMEDIATE $$
BEGIN
    IF ($is_managed_target) THEN
        GRANT INSERT, UPDATE, DELETE ON TABLE IDENTIFIER($src_fqn) TO ROLE AC_OWNER;
    END IF;
END;
$$;
