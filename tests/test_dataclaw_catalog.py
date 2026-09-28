"""Header catalog and cell-value set built from a DataClaw database directory."""

import json
import os

import pytest

from tkstore.dataclaw.catalog import build_catalog, load_catalog


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / "database"
    (root / "enterprise").mkdir(parents=True)
    (root / "enterprise" / "company_operation_status.csv").write_text(
        "\ufeffid,year,bmCode,targetName,targetUnit,value\n"
        "1,2022,BM0001,净利润额,十万元,12.5\n"
        "2,2022,BM0001,总资产金额,元,value\n"
        '3,2021,BM0002,"营业收入, 合计",元,  7  \n',
        encoding="utf-8",
    )
    (root / "internal_metrics.csv").write_text(
        "name,definition\nconcentration,top-4 share of an industry\n" + "x" * 41 + ",ab\n",
        encoding="utf-8",
    )
    (root / "bilingual_translation_english_chinese.json").write_text(
        json.dumps(
            {"company_items": {"海山昌工设备公司": ["Haishan Chang Company", "Haishan Co"]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def test_headers_for_csv_and_json_files(data_dir):
    catalog = build_catalog(data_dir)
    assert catalog.headers["enterprise/company_operation_status.csv"] == [
        "id", "year", "bmCode", "targetName", "targetUnit", "value",
    ]
    assert catalog.headers["internal_metrics.csv"] == ["name", "definition"]
    assert catalog.headers["bilingual_translation_english_chinese.json"] == []


def test_file_and_column_existence(data_dir):
    catalog = build_catalog(data_dir)
    assert catalog.has_column("enterprise/company_operation_status.csv", "targetUnit")
    assert not catalog.has_column("enterprise/company_operation_status.csv", "industry")
    assert catalog.has_file("bilingual_translation_english_chinese.json")
    assert not catalog.has_file("enterprise/company_profile.csv")


def test_value_set_keeps_3_to_40_characters_including_numbers(data_dir):
    values = build_catalog(data_dir).values
    assert "2022" in values
    assert "12.5" in values
    assert "BM0001" in values
    assert "净利润额" in values
    assert "营业收入, 合计" in values  # quoted CSV field with a comma stays whole
    assert "十万元" in values
    assert "元" not in values  # shorter than 3
    assert "7" not in values
    assert "ab" not in values
    assert "x" * 41 not in values  # longer than 40


def test_value_set_includes_json_strings(data_dir):
    values = build_catalog(data_dir).values
    assert "海山昌工设备公司" in values
    assert "Haishan Chang Company" in values


def test_values_equal_to_schema_names_are_excluded(data_dir):
    values = build_catalog(data_dir).values
    assert "value" not in values  # a cell equal to a column name
    assert "company_items" not in values  # top-level JSON keys are structure, not values


def test_find_cell_values_in_text(data_dir):
    catalog = build_catalog(data_dir)
    text = "比较 targetName 为净利润额的 value 前，先看 2022年 的 targetUnit"
    assert set(catalog.find_cell_values(text)) == {"净利润额", "2022"}


def test_find_cell_values_ignores_schema_names_and_clean_text(data_dir):
    catalog = build_catalog(data_dir)
    text = "跨不同 targetName 比较 value 前，按每行的 targetUnit 换算到同一单位"
    assert catalog.find_cell_values(text) == []


def test_load_catalog_caches_and_rebuilds_when_data_changes(data_dir, tmp_path):
    cache_dir = tmp_path / "cache"
    first = load_catalog(data_dir, cache_dir)
    assert "BM0009" not in first.values
    assert len(list(cache_dir.iterdir())) == 1

    again = load_catalog(data_dir, cache_dir)
    assert again.values == first.values and again.headers == first.headers

    csv_path = data_dir / "enterprise" / "company_operation_status.csv"
    with csv_path.open("a", encoding="utf-8") as fh:
        fh.write("4,2023,BM0009,净利润额,十万元,1\n")
    stat = csv_path.stat()
    os.utime(csv_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    rebuilt = load_catalog(data_dir, cache_dir)
    assert "BM0009" in rebuilt.values
