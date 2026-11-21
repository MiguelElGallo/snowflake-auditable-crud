-- Seed the DEMO tables (idempotent: only inserts into empty tables).
-- Run after the first deploy:  snow sql -f bootstrap/10_seed_demo.sql -c <conn> --role AC_OWNER

INSERT INTO AUDITABLE_CRUD.DEMO.REGIONS (CODE, NAME, RETIRED)
SELECT * FROM VALUES
    ('EU', 'Europe', FALSE), ('US', 'United States', FALSE), ('UK', 'United Kingdom', FALSE),
    ('APAC', 'Asia Pacific', FALSE), ('LATAM', 'Latin America', TRUE)
WHERE NOT EXISTS (SELECT 1 FROM AUDITABLE_CRUD.DEMO.REGIONS);

INSERT INTO AUDITABLE_CRUD.DEMO.PRICING_CONFIG (SKU, REGION, PRICE, CURRENCY, ACTIVE, UPDATED_AT)
SELECT
    'SKU-' || LPAD(s.i::VARCHAR, 4, '0'),
    r.CODE,
    ROUND(10 + UNIFORM(0, 990, RANDOM(42)) + UNIFORM(0, 99, RANDOM(7)) / 100, 2),
    IFF(r.CODE = 'EU', 'EUR', IFF(r.CODE = 'UK', 'GBP', 'USD')),
    r.CODE <> 'LATAM',
    CURRENT_TIMESTAMP()
FROM (SELECT SEQ4() + 1 AS i FROM TABLE(GENERATOR(ROWCOUNT => 200))) s
CROSS JOIN AUDITABLE_CRUD.DEMO.REGIONS r
WHERE NOT EXISTS (SELECT 1 FROM AUDITABLE_CRUD.DEMO.PRICING_CONFIG);

INSERT INTO AUDITABLE_CRUD.DEMO.FEATURE_FLAGS (FLAG_KEY, ENVIRONMENT, ENABLED, ROLLOUT_PCT, OWNER)
SELECT f.column1, e.column1, f.column2, f.column3, f.column4
FROM VALUES
    ('new_checkout', TRUE, 25, 'payments'), ('dark_mode', TRUE, 100, 'web'),
    ('ai_search', FALSE, 0, 'search'), ('bulk_export', TRUE, 50, 'data'),
    ('beta_pricing', FALSE, 0, 'pricing') AS f
CROSS JOIN (VALUES ('dev'), ('staging'), ('prod')) AS e
WHERE NOT EXISTS (SELECT 1 FROM AUDITABLE_CRUD.DEMO.FEATURE_FLAGS);
