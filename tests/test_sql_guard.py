import pytest

from auditable_crud.sql_guard import GuardError, TableRef, guard_dml, has_sequence_default

TARGET = TableRef("APP", "DEMO", "PRICING_CONFIG")
LOOKUP = TableRef("APP", "DEMO", "REGIONS")
CLONE = TableRef("APP", "SANDBOX", "PRICING_CONFIG__CR_X")


def test_sandbox_lookups_share_the_clone_timestamp() -> None:
    sql = (
        "UPDATE pricing_config SET active = FALSE WHERE region IN ("
        "WITH chosen AS (SELECT r.code FROM regions r WHERE r.retired) "
        "SELECT code FROM chosen UNION SELECT code FROM regions)"
    )
    statement = guard_dml(sql, TARGET, frozenset({LOOKUP}))
    preview = statement.render_for(CLONE, lookup_snapshot_ns=1234567890)
    historical = f"{LOOKUP.sql} AT (TIMESTAMP => TO_TIMESTAMP_LTZ(1234567890, 9))"
    assert preview.count(historical) == 2
    assert f"{historical} AS r" in preview and "FROM chosen" in preview
    assert f"UPDATE {CLONE.sql} SET" in preview
    assert " AT " not in statement.render_for(TARGET) and " AT " not in statement.canonical_sql


@pytest.mark.parametrize(
    "expression",
    [
        "DAYOFWEEK(updated_at)",
        "WEEKOFYEAR(updated_at)",
        "YEAROFWEEK(updated_at)",
        "EXTRACT(DOW FROM updated_at)",
        "DATE_PART('wy', updated_at)",
        "DATE_PART(yearofweek, updated_at)",
        "DATE_TRUNC('wk', updated_at)",
        "TRUNC(updated_at, 'WEEK')",
        "LAST_DAY(updated_at, 'WEEK')",
        "DATEDIFF(w, updated_at, updated_at)",
        "TIME_SLICE(updated_at, 1, 'WEEK')",
    ],
)
def test_calendar_functions_remain_available_in_the_fixed_dml_context(expression: str) -> None:
    statement = guard_dml(f"DELETE FROM pricing_config WHERE {expression} IS NULL", TARGET)
    assert guard_dml(statement.canonical_sql, TARGET).canonical_sql == statement.canonical_sql


@pytest.mark.parametrize(
    "expression",
    [
        "DAYOFWEEKISO(updated_at)",
        "WEEKISO(updated_at)",
        "YEAROFWEEKISO(updated_at)",
        "DATE_PART(week_iso, updated_at)",
        "EXTRACT(dayofweekiso FROM updated_at)",
        "DATEADD(week, 1, updated_at)",
        "DATE_TRUNC(month, updated_at)",
    ],
)
def test_fixed_iso_dates_and_calendar_arithmetic_reparse(expression: str) -> None:
    statement = guard_dml(f"DELETE FROM pricing_config WHERE {expression} IS NULL", TARGET)
    assert guard_dml(statement.canonical_sql, TARGET).canonical_sql == statement.canonical_sql


@pytest.mark.parametrize("suffix", ["LTZ", "NTZ", "TZ"])
@pytest.mark.parametrize("args", ["1", "price, 9"])
def test_epoch_conversion_keeps_the_requested_timestamp_type(suffix: str, args: str) -> None:
    conversion = f"TO_TIMESTAMP_{suffix}({args})"
    statement = guard_dml(f"UPDATE pricing_config SET updated_at = {conversion}", TARGET)
    assert conversion in statement.canonical_sql
    assert conversion in statement.render_for(CLONE)
    assert guard_dml(statement.canonical_sql, TARGET).canonical_sql == statement.canonical_sql


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("UPDATE pricing_config SET price = price * 1.1 WHERE region = 'EU';", "UPDATE"),
        ("insert into app.demo.pricing_config (sku, price) values ('A', 1)", "INSERT"),
        ("DELETE FROM demo.pricing_config WHERE price IS NULL", "DELETE"),
        (
            "MERGE INTO pricing_config t USING (SELECT 'A' sku, 2 price) s ON t.sku = s.sku "
            "WHEN MATCHED THEN UPDATE SET t.price = s.price",
            "MERGE",
        ),
        (
            "DELETE FROM pricing_config WHERE sku IN "
            "(WITH c AS (SELECT sku FROM pricing_config WHERE price < 0) SELECT sku FROM c)",
            "DELETE",
        ),
        ("UPDATE pricing_config SET h = HASH(sku), u = UPPER(region)", "UPDATE"),
        (
            "DELETE FROM pricing_config WHERE sku IN "
            "(WITH a AS (SELECT sku FROM pricing_config), b AS (SELECT sku FROM a) "
            "SELECT sku FROM b)",
            "DELETE",
        ),
    ],
)
def test_accepts_single_dml_on_target(sql: str, kind: str) -> None:
    assert guard_dml(sql, TARGET).kind == kind


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("", "empty"),
        ("UPDATE pricing_config SET a = 1; DROP TABLE pricing_config", "Exactly one"),
        ("SELECT * FROM pricing_config", "Only INSERT"),
        ("DROP TABLE pricing_config", "Only INSERT"),
        ("CALL app.core.sp_api('execute', NULL)", "Only INSERT"),
        ("EXECUTE IMMEDIATE 'DELETE FROM pricing_config'", "Only INSERT"),
        ("UPDATE other_table SET a = 1", "modifies APP.DEMO.OTHER_TABLE"),
        ("UPDATE app.demo2.pricing_config SET a = 1", "modifies"),
        ("INSERT OVERWRITE INTO pricing_config SELECT 1", "OVERWRITE"),
        ("INSERT ALL INTO pricing_config VALUES (1) SELECT 1", "Only INSERT"),
        ("UPDATE pricing_config SET a = SYSTEM$WAIT(10)", "SYSTEM$WAIT"),
        ("UPDATE pricing_config SET a = app.util.leak(b)", "LEAK"),
        ("DELETE FROM pricing_config WHERE sku IN (SELECT sku FROM secrets.s.cards)", "Reading"),
        (
            "DELETE FROM pricing_config WHERE sku IN "
            "(SELECT value FROM TABLE(FLATTEN(input => PARSE_JSON('[1]'))))",
            "TableFromRows",
        ),
        (
            "UPDATE pricing_config SET a = (SELECT MAX(a) FROM pricing_config AT(OFFSET => -60))",
            "Time",
        ),
        ("UPDATE pricing_config SET a = ", "parse"),
        ("UPDATE pricing_config SET region = 'EU", "parse"),
        ("WITH c AS (SELECT 1) DELETE FROM pricing_config", "WITH inside"),
        ("UPDATE IDENTIFIER('app.demo.pricing_config') SET a = 1", "Could not determine"),
        (
            "DELETE FROM pricing_config WHERE sku IN "
            "(SELECT sku FROM IDENTIFIER('secrets.s.cards'))",
            "computed table",
        ),
        ("INSERT INTO pricing_config SELECT $1, $2 FROM @app.demo.stage", "computed table"),
        (
            "INSERT INTO pricing_config SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
            "TableFromRows",
        ),
        ("UPDATE pricing_config SET price = AI_COMPLETE('m', 'x')", "AI_COMPLETE"),
        # A CTE name only hides tables where the CTE is in scope.
        (
            "UPDATE pricing_config SET note = (SELECT secret FROM payroll LIMIT 1) "
            "WHERE EXISTS (WITH payroll AS (SELECT 1 x) SELECT 1 FROM payroll)",
            "Reading APP.DEMO.PAYROLL",
        ),
        (
            "DELETE FROM pricing_config WHERE sku IN "
            "(WITH a AS (SELECT * FROM payroll), payroll AS (SELECT 1 x) SELECT x FROM a)",
            "Reading APP.DEMO.PAYROLL",
        ),
        (
            "DELETE FROM pricing_config WHERE sku IN "
            "(WITH payroll AS (SELECT * FROM payroll) SELECT 1 FROM payroll)",
            "Recursive CTE",
        ),
    ],
)
def test_rejects_unsafe_or_off_target_sql(sql: str, reason: str) -> None:
    with pytest.raises(GuardError, match=reason.replace("$", r"\$")):
        guard_dml(sql, TARGET)


def test_hidden_statement_in_comment_is_inert() -> None:
    guarded = guard_dml("UPDATE pricing_config SET a = 1 /* ; DROP TABLE x */", TARGET)
    rendered = guarded.render_for(CLONE)
    assert rendered.startswith('UPDATE "APP"."SANDBOX"."PRICING_CONFIG__CR_X" SET a = 1')


def test_allowed_reads_are_qualified_and_reported() -> None:
    guarded = guard_dml(
        "UPDATE pricing_config p SET price = 0 "
        "WHERE region IN (SELECT code FROM regions WHERE retired)",
        TARGET,
        allowed_reads=frozenset({LOOKUP}),
    )
    assert guarded.reads == frozenset({LOOKUP})
    assert '"APP"."DEMO"."REGIONS"' in guarded.canonical_sql


@pytest.mark.parametrize("relation", ["pricing_config", "regions"])
@pytest.mark.parametrize(
    "historical",
    [
        "AT(OFFSET => -60)",
        "BEFORE(TIMESTAMP => TO_TIMESTAMP_NTZ('2025-01-01'))",
        "CHANGES(INFORMATION => DEFAULT) AT(OFFSET => -60)",
    ],
)
def test_all_physical_reads_use_current_data(relation: str, historical: str) -> None:
    sql = (
        f"UPDATE pricing_config SET price = 1 WHERE EXISTS (SELECT 1 FROM {relation} {historical})"
    )
    with pytest.raises(GuardError, match="Time Travel and CHANGES"):
        guard_dml(sql, TARGET, frozenset({LOOKUP}))


@pytest.mark.parametrize(
    "selection",
    [
        "SELECT code FROM regions SAMPLE ROW (1 ROWS)",
        "SELECT code FROM regions TABLESAMPLE SYSTEM (10) SEED (5)",
        "SELECT code FROM regions WHERE RANDOM(7) > 0",
        "SELECT code FROM regions WHERE UUID_STRING() = code",
        "SELECT TOP 1 code FROM regions",
        "SELECT code FROM regions ORDER BY name LIMIT 1",
        "SELECT code FROM regions ORDER BY name OFFSET 1 ROWS FETCH NEXT 1 ROWS ONLY",
        "SELECT ANY_VALUE(code) FROM regions",
        "SELECT MAX_BY(code, name) FROM regions",
        "SELECT code FROM regions QUALIFY ROW_NUMBER() OVER (ORDER BY name) = 1",
        "SELECT code FROM regions QUALIFY COUNT(*) OVER "
        "(ORDER BY name ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) = 1",
    ],
)
def test_selection_cannot_depend_on_sampling_or_physical_row_order(selection: str) -> None:
    with pytest.raises(GuardError, match="repeatable"):
        guard_dml(
            f"DELETE FROM pricing_config WHERE region IN ({selection})", TARGET, frozenset({LOOKUP})
        )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM pricing_config WHERE updated_at < CURRENT_TIMESTAMP",
        "UPDATE pricing_config SET price = 1 WHERE updated_at < DATEADD(day, -1, CURRENT_DATE())",
        "DELETE FROM pricing_config WHERE EXISTS (WITH cutoff AS (SELECT GETDATE() AS ts) "
        "SELECT 1 FROM cutoff WHERE cutoff.ts > updated_at)",
        "MERGE INTO pricing_config p USING regions r ON p.region = r.code "
        "WHEN MATCHED AND p.updated_at < SYSDATE() THEN UPDATE SET price = 1",
        "INSERT INTO pricing_config (sku) SELECT code FROM regions "
        "WHERE CURRENT_TIME() > '12:00:00'",
    ],
)
def test_clocks_cannot_select_rows_even_through_query_aliases(sql: str) -> None:
    with pytest.raises(GuardError, match="fixed date or time"):
        guard_dml(sql, TARGET, frozenset({LOOKUP}))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE pricing_config SET updated_at = CURRENT_TIMESTAMP() WHERE sku = 'A'",
        "INSERT INTO pricing_config (sku, updated_at) VALUES ('A', CURRENT_TIMESTAMP())",
        "MERGE INTO pricing_config p USING regions r ON p.region = r.code "
        "WHEN MATCHED THEN UPDATE SET updated_at = CURRENT_TIMESTAMP() "
        "WHEN NOT MATCHED THEN INSERT (sku, updated_at) VALUES (r.code, CURRENT_TIMESTAMP())",
        "DELETE FROM pricing_config WHERE updated_at < TO_TIMESTAMP_NTZ('2025-01-01')",
        "UPDATE pricing_config SET sku = UUID_STRING('cba30d00-f50f-4b74-b559-7d4eca09b729', sku)",
        "DELETE FROM pricing_config WHERE region IN (SELECT MIN(code) FROM regions)",
    ],
)
def test_fixed_selection_preserves_metadata_clocks_and_named_identifiers(sql: str) -> None:
    guarded = guard_dml(sql, TARGET, frozenset({LOOKUP}))
    assert guard_dml(guarded.canonical_sql, TARGET, frozenset({LOOKUP})).render_for(
        CLONE
    ) == guarded.render_for(CLONE)


def test_render_rewrites_every_target_reference_but_not_reads() -> None:
    guarded = guard_dml(
        "DELETE FROM pricing_config WHERE sku IN "
        "(SELECT sku FROM pricing_config JOIN regions USING (region) WHERE retired)",
        TARGET,
        allowed_reads=frozenset({LOOKUP}),
    )
    rendered = guarded.render_for(CLONE)
    assert rendered.count(CLONE.sql) == 2
    assert TARGET.sql not in rendered
    assert LOOKUP.sql in rendered


def test_render_rewrites_columns_qualified_by_target_name() -> None:
    guarded = guard_dml(
        "UPDATE pricing_config SET price = pricing_config.price + 1 "
        "WHERE demo.pricing_config.region = 'EU'",
        TARGET,
    )
    rendered = guarded.render_for(CLONE)
    assert "pricing_config." not in rendered.lower().replace(CLONE.sql.lower(), "")
    assert f"{CLONE.sql}.price" in rendered
    assert f"{CLONE.sql}.region" in rendered


def test_render_keeps_alias_qualified_columns() -> None:
    guarded = guard_dml(
        "MERGE INTO pricing_config t USING regions r ON t.region = r.code "
        "WHEN MATCHED THEN UPDATE SET t.price = 0",
        TARGET,
        allowed_reads=frozenset({LOOKUP}),
    )
    rendered = guarded.render_for(CLONE)
    assert f"{CLONE.sql} AS t" in rendered
    assert "t.region = r.code" in rendered


def test_cte_named_like_the_target_stays_a_cte_in_the_sandbox() -> None:
    guarded = guard_dml(
        "INSERT INTO pricing_config (sku) "
        "WITH pricing_config AS (SELECT 'A' AS sku) "
        "SELECT pricing_config.sku FROM pricing_config",
        TARGET,
    )
    assert guarded.canonical_sql.startswith("INSERT INTO " + TARGET.sql)
    assert guarded.render_for(CLONE).endswith("SELECT pricing_config.sku FROM pricing_config")
    assert guarded.render_for(CLONE).count(CLONE.sql) == 1
    assert guard_dml(guarded.canonical_sql, TARGET).render_for(CLONE) == guarded.render_for(CLONE)


def test_outer_target_and_inner_alias_with_the_same_name_resolve_independently() -> None:
    guarded = guard_dml(
        "UPDATE pricing_config SET price = pricing_config.price + 1 "
        "WHERE region IN (SELECT pricing_config.code FROM regions AS pricing_config)",
        TARGET,
        frozenset({LOOKUP}),
    )
    rendered = guarded.render_for(CLONE)
    assert f"SET price = {CLONE.sql}.price + 1" in rendered
    assert f"SELECT pricing_config.code FROM {LOOKUP.sql} AS pricing_config" in rendered


@pytest.mark.parametrize("qualifier", ["regions", "data.regions", "reference.data.regions"])
def test_column_of_a_cross_database_lookup_binds_to_the_declared_table(qualifier: str) -> None:
    external = TableRef("REFERENCE", "DATA", "REGIONS")
    guarded = guard_dml(
        "DELETE FROM pricing_config WHERE region IN "
        f"(SELECT {qualifier}.code FROM reference.data.regions)",
        TARGET,
        frozenset({external}),
    )
    assert f"SELECT {external.sql}.code FROM {external.sql}" in guarded.render_for(CLONE)


def test_ambiguous_lookup_names_require_explicit_qualification() -> None:
    external = TableRef("REFERENCE", "DATA", "REGIONS")
    with pytest.raises(GuardError, match="Ambiguous column qualifier"):
        guard_dml(
            "DELETE FROM pricing_config WHERE region IN "
            "(SELECT regions.code FROM app.demo.regions CROSS JOIN reference.data.regions)",
            TARGET,
            frozenset({LOOKUP, external}),
        )


def test_database_qualification_requires_an_explicit_schema() -> None:
    with pytest.raises(GuardError, match="schema explicitly"):
        guard_dml("UPDATE app..pricing_config SET price = 1", TARGET)


@pytest.mark.parametrize(
    "expr",
    [
        "IFF(active AND price > 0, ROUND(price, 2), NULLIFZERO(price))",
        "CASE WHEN region = 'EU' OR NOT active THEN LOWER(sku) ELSE TRIM(currency) END",
        "DATEADD(day, 2, CURRENT_TIMESTAMP())",
        "TRY_TO_TIMESTAMP_LTZ('2026-11-21 10:20:30')",
        "TO_TIMESTAMP_NTZ('2026-11-21', 'YYYY-MM-DD')",
        "COALESCE((SELECT MAX(price) FROM pricing_config), 0)",
        "OBJECT_CONSTRUCT('sku', sku, 'region', region)",
    ],
)
def test_reviewed_scalar_aggregate_and_conversion_functions_are_accepted(expr: str) -> None:
    assert guard_dml(f"UPDATE pricing_config SET a = {expr}", TARGET).kind == "UPDATE"


def test_parser_recognition_alone_does_not_admit_an_unreviewed_function() -> None:
    with pytest.raises(GuardError, match="not an allowed built-in"):
        guard_dml("UPDATE pricing_config SET price = CORR(price, price)", TARGET)


@pytest.mark.parametrize(
    ("default", "shared_sequence"),
    [
        (None, False),
        ("", False),
        ("'docs.example.NEXTVAL'", False),
        ("CURRENT_TIMESTAMP()", False),
        ("IDENTITY START 1 INCREMENT 1 NOORDER", False),
        ("AUTOINCREMENT(1, 5) ORDER", False),
        ('CAST("App"."Demo"."Item Ids".NEXTVAL AS NUMBER)', True),
        ("demo_ids.nextval", True),
        ("'", True),
    ],
)
def test_sequence_defaults_are_identified_from_snowflake_metadata(
    default: str | None, shared_sequence: bool
) -> None:
    assert has_sequence_default(default) is shared_sequence


def test_canonical_sql_is_deterministic_and_reparses() -> None:
    first = guard_dml("update PRICING_CONFIG set price=1", TARGET).canonical_sql
    again = guard_dml(first, TARGET).canonical_sql
    assert first == again == 'UPDATE "APP"."DEMO"."PRICING_CONFIG" SET price = 1'


def test_quoted_mixed_case_target_is_distinct() -> None:
    with pytest.raises(GuardError, match="modifies"):
        guard_dml('UPDATE "pricing_config" SET a = 1', TARGET)


def test_table_ref_parse_validates() -> None:
    assert TableRef.parse("app.demo.t") == TableRef("APP", "DEMO", "T")
    with pytest.raises(GuardError):
        TableRef.parse("demo.t")
