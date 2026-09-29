"""TABLES / COLUMNS parsing and SCOPE derivation for DataClaw divergences and rules."""

import pytest

from tkstore.dataclaw.scope import (
    ColumnRef,
    check_scope_consistency,
    derive_scope,
    parse_columns,
    parse_tables,
)

OPS = "enterprise/company_operation_status.csv"
PROFILE = "enterprise/company_profile.csv"
TRANSLATION = "bilingual_translation_english_chinese.json"


def test_parse_single_column():
    assert parse_columns(f"{OPS}.targetName") == [ColumnRef(OPS, "targetName")]


def test_parse_multiple_columns_across_lines_and_commas():
    text = f"{OPS}.targetName,\n   {OPS}.targetUnit, {PROFILE}.bmCode"
    assert parse_columns(text) == [
        ColumnRef(OPS, "targetName"),
        ColumnRef(OPS, "targetUnit"),
        ColumnRef(PROFILE, "bmCode"),
    ]


def test_parse_columns_strips_database_prefix_and_dedupes():
    text = f"./database/{OPS}.value, database/{OPS}.value"
    assert parse_columns(text) == [ColumnRef(OPS, "value")]


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_parse_empty_columns(text):
    assert parse_columns(text) == []


@pytest.mark.parametrize(
    "text",
    [
        PROFILE,  # a file belongs in TABLES
        f"{PROFILE}.all",  # the old whole-file sentinel is gone
        "company_profile.targetName",  # no .csv/.json extension
        f"{OPS}.",  # empty column name
    ],
)
def test_parse_columns_rejects_malformed_entries(text):
    with pytest.raises(ValueError):
        parse_columns(text)


def test_parse_tables():
    text = f"{PROFILE},\n ./database/{TRANSLATION}, database/{PROFILE}"
    assert parse_tables(text) == [PROFILE, TRANSLATION]


@pytest.mark.parametrize("text", ["", "  \n "])
def test_parse_empty_tables(text):
    assert parse_tables(text) == []


@pytest.mark.parametrize("text", [f"{OPS}.value", "company_profile", "enterprise/"])
def test_parse_tables_rejects_non_files(text):
    with pytest.raises(ValueError):
        parse_tables(text)


@pytest.mark.parametrize(
    "tables, columns, scope",
    [
        ([], [], "generic"),
        ([], [ColumnRef(OPS, "value")], "column"),
        ([], [ColumnRef(OPS, "targetName"), ColumnRef(OPS, "value")], "multi_column"),
        ([OPS], [], "file"),
        ([OPS], [ColumnRef(OPS, "value")], "file"),  # whole file plus one of its columns
        ([], [ColumnRef(OPS, "bmCode"), ColumnRef(PROFILE, "bmCode")], "cross_table"),
        ([OPS, PROFILE], [], "cross_table"),
        ([TRANSLATION], [ColumnRef(PROFILE, "bmCompanyName")], "cross_table"),
    ],
)
def test_derive_scope(tables, columns, scope):
    assert derive_scope(tables, columns) == scope


def test_consistent_scope_passes():
    columns = [ColumnRef(OPS, "targetName"), ColumnRef(OPS, "value")]
    assert check_scope_consistency("multi_column", [], columns) is None
    assert check_scope_consistency("  Multi_Column ", [], columns) is None


def test_inconsistent_scope_reports_both_scopes():
    error = check_scope_consistency("column", [PROFILE], [ColumnRef(OPS, "bmCode")])
    assert error is not None
    assert "column" in error and "cross_table" in error


def test_unknown_declared_scope_is_an_error():
    assert check_scope_consistency("table", [], [ColumnRef(OPS, "value")]) is not None
