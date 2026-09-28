"""COLUMNS parsing and SCOPE derivation for DataClaw divergences and rules."""

import pytest

from tkstore.dataclaw.scope import (
    ColumnRef,
    check_scope_consistency,
    derive_scope,
    parse_columns,
)

OPS = "enterprise/company_operation_status.csv"
PROFILE = "enterprise/company_profile.csv"


def test_parse_single_column():
    assert parse_columns(f"{OPS}.targetName") == [ColumnRef(OPS, "targetName")]


def test_parse_multiple_columns_across_lines_and_commas():
    text = f"{OPS}.targetName,\n   {OPS}.targetUnit, {PROFILE}.bmCode"
    assert parse_columns(text) == [
        ColumnRef(OPS, "targetName"),
        ColumnRef(OPS, "targetUnit"),
        ColumnRef(PROFILE, "bmCode"),
    ]


def test_parse_whole_file_and_json_file():
    assert parse_columns("bilingual_translation_english_chinese.json.all") == [
        ColumnRef("bilingual_translation_english_chinese.json", "all")
    ]


def test_parse_strips_database_prefix_and_dedupes():
    text = f"./database/{OPS}.value, database/{OPS}.value"
    assert parse_columns(text) == [ColumnRef(OPS, "value")]


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_parse_empty_is_generic(text):
    assert parse_columns(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "enterprise/company_profile.csv",  # file without a column or .all
        "company_profile.targetName",  # no .csv/.json extension
        f"{OPS}.",  # empty column name
    ],
)
def test_parse_rejects_malformed_entries(text):
    with pytest.raises(ValueError):
        parse_columns(text)


@pytest.mark.parametrize(
    "refs, scope",
    [
        ([], "generic"),
        ([ColumnRef(OPS, "value")], "column"),
        ([ColumnRef(OPS, "targetName"), ColumnRef(OPS, "value")], "multi_column"),
        ([ColumnRef(OPS, "all")], "file"),
        ([ColumnRef(OPS, "bmCode"), ColumnRef(PROFILE, "bmCode")], "cross_table"),
        ([ColumnRef(OPS, "all"), ColumnRef(PROFILE, "all")], "cross_table"),
    ],
)
def test_derive_scope(refs, scope):
    assert derive_scope(refs) == scope


def test_derive_scope_rejects_whole_file_mixed_with_its_columns():
    with pytest.raises(ValueError):
        derive_scope([ColumnRef(OPS, "all"), ColumnRef(OPS, "value")])


def test_consistent_scope_passes():
    refs = [ColumnRef(OPS, "targetName"), ColumnRef(OPS, "value")]
    assert check_scope_consistency("multi_column", refs) is None
    assert check_scope_consistency("  Multi_Column ", refs) is None


def test_inconsistent_scope_reports_both_scopes():
    refs = [ColumnRef(OPS, "bmCode"), ColumnRef(PROFILE, "bmCode")]
    error = check_scope_consistency("column", refs)
    assert error is not None
    assert "column" in error and "cross_table" in error


def test_unknown_declared_scope_is_an_error():
    assert check_scope_consistency("table", [ColumnRef(OPS, "value")]) is not None
