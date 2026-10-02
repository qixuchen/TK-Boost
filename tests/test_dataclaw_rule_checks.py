"""File and column existence checks against the database catalog."""

import pytest

from tkstore.dataclaw.catalog import Catalog
from tkstore.dataclaw.scope import ColumnRef, validate_refs

OPS = "enterprise/company_operation_status.csv"
TRANSLATION = "bilingual_translation_english_chinese.json"


@pytest.fixture
def catalog():
    return Catalog(headers={OPS: ["bmCode", "targetName", "targetUnit", "value"], TRANSLATION: []})


def test_existing_tables_and_columns_pass(catalog):
    assert validate_refs([OPS, TRANSLATION], [ColumnRef(OPS, "targetUnit")], catalog) == []


def test_unknown_table_column_and_column_file_are_reported(catalog):
    errors = validate_refs(
        ["policy/policy_resource.csv"],
        [ColumnRef(OPS, "industry"), ColumnRef("enterprise/company_profile.csv", "bmCode")],
        catalog,
    )
    assert len(errors) == 3
    assert any("policy_resource.csv" in e for e in errors)
    assert any("industry" in e for e in errors)
    assert any("company_profile.csv" in e for e in errors)


def test_json_file_has_no_columns(catalog):
    assert validate_refs([], [ColumnRef(TRANSLATION, "company_items")], catalog)
