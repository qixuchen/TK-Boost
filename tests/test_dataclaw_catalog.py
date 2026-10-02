"""Header catalog built from a DataClaw database directory."""

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


def test_load_catalog_caches_and_rebuilds_when_data_changes(data_dir, tmp_path):
    cache_dir = tmp_path / "cache"
    first = load_catalog(data_dir, cache_dir)
    assert len(list(cache_dir.iterdir())) == 1

    again = load_catalog(data_dir, cache_dir)
    assert again.headers == first.headers

    metrics = data_dir / "internal_metrics.csv"
    metrics.write_text("name,definition,source\nconcentration,top-4 share,report\n", encoding="utf-8")
    stat = metrics.stat()
    os.utime(metrics, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    rebuilt = load_catalog(data_dir, cache_dir)
    assert rebuilt.headers["internal_metrics.csv"] == ["name", "definition", "source"]
    assert len(list(cache_dir.iterdir())) == 1


def test_cache_holds_only_headers(data_dir, tmp_path):
    cache_dir = tmp_path / "cache"
    load_catalog(data_dir, cache_dir)
    (cached,) = cache_dir.iterdir()
    assert set(json.loads(cached.read_text(encoding="utf-8"))) == {"headers"}
