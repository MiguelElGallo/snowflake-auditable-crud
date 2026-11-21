from datetime import datetime

import pytest

from auditable_crud.naming import (
    MAX_IDENTIFIER_LENGTH,
    IdentifierError,
    SandboxNames,
    build_sandbox_names,
    fqn,
    normalize_identifier,
    quote_identifier,
    sql_string_literal,
)

TS = datetime(2026, 11, 21, 13, 5, 9)


def test_sandbox_names_are_identifiable() -> None:
    names = build_sandbox_names("pricing_config", "demo.user@example.invalid", "ab12cd34-ffff", TS)
    assert names.clone == "PRICING_CONFIG__CR_20261121130509_DEMO_USER_EXAMPLE_INVALID_AB12CD34FFFF"
    assert names.stream == names.clone + "_STRM"
    assert names.reval_diff == names.clone + "_RV_DIFF"


def test_concurrent_requests_with_a_common_uuid_prefix_get_distinct_clones() -> None:
    first = build_sandbox_names("PRICES", "Ana", "12345600-0000-4000-a000-000000000001", TS)
    second = build_sandbox_names("PRICES", "Ana", "12345600-0000-4000-a000-000000000002", TS)
    assert first.clone != second.clone


def test_revision_names_preserve_provenance_and_fit_all_snowflake_objects() -> None:
    legacy_tail = "__CR_20261121130509_DEVELOPER_ABCDEF"
    long_base = "T" * (MAX_IDENTIFIER_LENGTH - len("_RV_STRM") - len(legacy_tail)) + legacy_tail
    original = SandboxNames(long_base)
    assert original.for_revision(0) is original
    revised = original.for_revision(10**38 - 1)
    assert len(revised.reval_stream) <= MAX_IDENTIFIER_LENGTH
    assert revised.clone.endswith(f"{legacy_tail}_R{10**38 - 1}")
    assert revised.clone != original.for_revision(1).clone
    alternate = SandboxNames(long_base[:24] + "X" + long_base[25:])
    assert revised.clone != alternate.for_revision(10**38 - 1).clone


def test_published_objects_follow_independent_dry_run_and_review_revisions() -> None:
    names = SandboxNames("PRICES__CR_EXAMPLE")
    for kind in ("clone", "stream", "diff"):
        reval_kind = f"reval_{kind}"
        assert names.published({}, kind) == getattr(names, kind)
        assert names.published({}, reval_kind) == getattr(names, reval_kind)
        row = {"DRY_SANDBOX_REVISION": 2, "REVAL_SANDBOX_REVISION": 7}
        assert names.published(row, kind) == getattr(names.for_revision(2), kind)
        assert names.published(row, reval_kind) == getattr(names.for_revision(7), kind)
    with pytest.raises(ValueError, match="object kind"):
        names.published({}, "missing")


def test_every_derived_name_fits_snowflake_limit() -> None:
    names = build_sandbox_names("T" * 400, "U" * 400, "f" * 32, TS)
    derived = [names.clone, names.stream, names.diff, names.reval_clone, names.reval_stream]
    assert all(len(n) <= MAX_IDENTIFIER_LENGTH for n in [*derived, names.reval_diff])


def test_hostile_user_name_cannot_inject_sql() -> None:
    names = build_sandbox_names("T", 'x"; DROP TABLE y; --', "1", TS)
    assert set(names.clone) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def test_quoting_escapes_embedded_quotes() -> None:
    assert quote_identifier('a"b') == '"a""b"'
    assert fqn("DB", "S", "T") == '"DB"."S"."T"'


@pytest.mark.parametrize("bad", ["", "1abc", "a b", 'a"b', "a;b", "x" * 300])
def test_normalize_rejects_unsafe_identifiers(bad: str) -> None:
    with pytest.raises(IdentifierError):
        normalize_identifier(bad)


def test_normalize_upper_cases() -> None:
    assert normalize_identifier(" my_Table ") == "MY_TABLE"


@pytest.mark.parametrize(
    ("value", "literal"),
    [("plain", "'plain'"), ("o'brien", "'o''brien'"), ("a\\'; DROP", "'a\\\\''; DROP'")],
)
def test_sql_string_literal_escapes_quotes_and_backslashes(value: str, literal: str) -> None:
    assert sql_string_literal(value) == literal
