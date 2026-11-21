-- Sample managed tables. CHANGE_TRACKING is required for drift detection.
-- Seed data: bootstrap/10_seed_demo.sql

DEFINE TABLE {{ app_db }}.DEMO.PRICING_CONFIG (
    SKU        VARCHAR       NOT NULL,
    REGION     VARCHAR       NOT NULL,
    PRICE      NUMBER(12, 2) NOT NULL,
    CURRENCY   VARCHAR(3)    NOT NULL,
    ACTIVE     BOOLEAN       NOT NULL,
    UPDATED_AT TIMESTAMP_LTZ
)
CHANGE_TRACKING = TRUE
COMMENT = 'Demo: per-region list prices';

DEFINE TABLE {{ app_db }}.DEMO.REGIONS (
    CODE    VARCHAR NOT NULL,
    NAME    VARCHAR NOT NULL,
    RETIRED BOOLEAN NOT NULL
)
CHANGE_TRACKING = TRUE
COMMENT = 'Demo: lookup table that PRICING_CONFIG change requests may read';

DEFINE TABLE {{ app_db }}.DEMO.FEATURE_FLAGS (
    FLAG_KEY    VARCHAR    NOT NULL,
    ENVIRONMENT VARCHAR    NOT NULL,
    ENABLED     BOOLEAN    NOT NULL,
    ROLLOUT_PCT NUMBER(3)  NOT NULL,
    OWNER       VARCHAR
)
CHANGE_TRACKING = TRUE
COMMENT = 'Demo: feature flags per environment';
