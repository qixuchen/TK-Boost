"""Column-existence and rule-body checks against the database catalog."""

import pytest

from tkstore.dataclaw.catalog import Catalog
from tkstore.dataclaw.scope import ColumnRef, check_body, validate_columns

OPS = "enterprise/company_operation_status.csv"
TRANSLATION = "bilingual_translation_english_chinese.json"


@pytest.fixture
def catalog():
    return Catalog(
        headers={OPS: ["bmCode", "targetName", "targetUnit", "value"], TRANSLATION: []},
        values=frozenset({"净利润额", "十万元", "2022", "BM0001"}),
    )


def test_existing_columns_and_whole_files_pass(catalog):
    refs = [ColumnRef(OPS, "targetUnit"), ColumnRef(OPS, "all"), ColumnRef(TRANSLATION, "all")]
    assert validate_columns(refs, catalog) == []


def test_unknown_file_and_column_are_reported(catalog):
    errors = validate_columns(
        [ColumnRef(OPS, "industry"), ColumnRef("enterprise/company_profile.csv", "bmCode")],
        catalog,
    )
    assert len(errors) == 2
    assert any("industry" in e for e in errors)
    assert any("company_profile.csv" in e for e in errors)


def test_json_file_has_no_columns(catalog):
    assert validate_columns([ColumnRef(TRANSLATION, "company_items")], catalog)


def test_clean_body_passes(catalog):
    rule = {
        "ensure": "跨不同 targetName 比较或加总 value 前，按每行的 targetUnit 换算到同一单位",
        "when_to_check": "题目涉及同一文件中多个指标的比较、求和或比值",
        "context": "该文件是长表，每个指标一行，targetUnit 随 targetName 变化",
        "example_usage": "例如净利润额的单位是十万元，2022 年的值要先换算",
    }
    assert check_body(rule, catalog) == []


def test_values_in_body_fields_are_reported_per_field(catalog):
    rule = {
        "ensure": "净利润额的单位是十万元",
        "when_to_check": "题目问 2022 年的数据",
        "context": "长表",
        "example_usage": "BM0001",
    }
    errors = check_body(rule, catalog)
    assert len(errors) == 2
    ensure_error = next(e for e in errors if e.startswith("ENSURE"))
    assert "净利润额" in ensure_error and "十万元" in ensure_error
    assert any(e.startswith("WHEN_TO_CHECK") and "2022" in e for e in errors)


def test_missing_body_field_is_treated_as_empty(catalog):
    assert check_body({"ensure": "按 targetUnit 换算"}, catalog) == []
