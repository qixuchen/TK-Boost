"""Batch populate over a train split.

The CLI must refuse to learn from anything outside the split file, rebuild the
store on each full run, and leave a report even when some train ids have not
been run yet.
"""

import json
from pathlib import Path

import pytest

from tkboost import TKStore
from tkstore.populate import SplitLeakError, populate_split


def _write_split(path: Path, ids):
    path.write_text("\n".join(["# train"] + list(ids)) + "\n", encoding="utf-8")
    return path


def _write_output(outputs_base: Path, instance_id: str, stamp="20260911_120000"):
    out = outputs_base / f"{instance_id}_{stamp}"
    out.mkdir(parents=True)
    (out / "execution_query.sql").write_text("SELECT 1;", encoding="utf-8")
    return out


def _seed_store(path: Path):
    store = TKStore(str(path))
    from tkboost import TKStoreEntry

    store.insert(
        TKStoreEntry(
            instance_id="stale",
            db="OldDB",
            scope="generic",
            sql_operations="select",
            rule="leftover from a previous run",
        )
    )


@pytest.fixture
def stub_single(monkeypatch):
    calls = []

    def fake_populate(**kwargs):
        calls.append(kwargs)
        iid = kwargs["instance_id"]
        return {
            "instance_id": iid,
            "engine": "sqlite",
            "store": kwargs["store"],
            "db": "TestDB",
            "rule_count": 2 if iid == "local900" else 0,
            "skipped": None if iid == "local900" else "correct",
            "inserted": [],
            "diff_text": "",
            "tagged": {},
        }

    monkeypatch.setattr("tkstore.populate.populate_from_output_dir", fake_populate)
    return calls


def test_refuses_an_output_that_is_not_in_the_split(tmp_path, stub_single):
    """The last leak gate: a test instance under --outputs-base must abort the
    whole run rather than silently enter the store."""
    outputs = tmp_path / "outputs" / "train"
    _write_output(outputs, "local900")
    _write_output(outputs, "local999")
    split = _write_split(tmp_path / "train.txt", ["local900"])
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"
    _seed_store(store)

    with pytest.raises(SplitLeakError) as exc:
        populate_split(
            outputs_base=str(outputs),
            split_file=str(split),
            jsonl_path=str(tmp_path / "unused.jsonl"),
            store=str(store),
        )

    assert "local999" in str(exc.value)
    assert stub_single == []
    assert {r["instance_id"] for r in TKStore(str(store)).rows()} == {"stale"}


def test_rebuilds_the_store_before_populating(tmp_path, stub_single):
    outputs = tmp_path / "outputs" / "train"
    _write_output(outputs, "local900")
    split = _write_split(tmp_path / "train.txt", ["local900"])
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"
    _seed_store(store)

    populate_split(
        outputs_base=str(outputs),
        split_file=str(split),
        jsonl_path=str(tmp_path / "unused.jsonl"),
        store=str(store),
        rebuild=True,
    )

    assert {r["instance_id"] for r in TKStore(str(store)).rows()} == set()
    assert stub_single[0]["instance_id"] == "local900"
    assert stub_single[0]["output_dir"].endswith("local900_20260911_120000")


def test_populate_split_does_not_pre_resolve_the_sqlite_path(tmp_path, stub_single, monkeypatch):
    """Path resolution belongs in populate_from_output_dir, which has the db name."""
    outputs = tmp_path / "outputs" / "train"
    _write_output(outputs, "minidev0000")
    split = _write_split(tmp_path / "train.txt", ["minidev0000"])
    store = tmp_path / "artifacts" / "tkstore_bird.csv"
    monkeypatch.setattr(
        "tkstore.populate.resolve_sqlite_db_path",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("populate_split must not resolve the db path")
        ),
    )

    populate_split(
        outputs_base=str(outputs),
        split_file=str(split),
        jsonl_path=str(tmp_path / "unused.jsonl"),
        store=str(store),
    )

    assert stub_single[0].get("db_path_or_cred") is None


def test_records_train_ids_that_have_no_output_yet(tmp_path, stub_single):
    outputs = tmp_path / "outputs" / "train"
    _write_output(outputs, "local900")
    split = _write_split(tmp_path / "train.txt", ["local900", "local901"])
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    report = populate_split(
        outputs_base=str(outputs),
        split_file=str(split),
        jsonl_path=str(tmp_path / "unused.jsonl"),
        store=str(store),
    )

    by_id = {row["instance_id"]: row for row in report["instances"]}
    assert by_id["local900"]["rule_count"] == 2
    assert by_id["local901"]["skipped"] == "missing_output"
    assert by_id["local901"]["rule_count"] == 0
    assert [c["instance_id"] for c in stub_single] == ["local900"]


def test_skips_an_empty_directory_for_a_train_id(tmp_path, stub_single):
    outputs = tmp_path / "outputs" / "train"
    (outputs / "local900_20260911_110000").mkdir(parents=True)
    completed = _write_output(outputs, "local900", stamp="20260911_120000")
    split = _write_split(tmp_path / "train.txt", ["local900"])
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    populate_split(
        outputs_base=str(outputs),
        split_file=str(split),
        jsonl_path=str(tmp_path / "unused.jsonl"),
        store=str(store),
    )

    assert stub_single[0]["output_dir"] == str(completed)


def test_writes_a_report_next_to_the_outputs(tmp_path, stub_single):
    outputs = tmp_path / "outputs" / "train"
    _write_output(outputs, "local900")
    split = _write_split(tmp_path / "train.txt", ["local900"])
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    result = populate_split(
        outputs_base=str(outputs),
        split_file=str(split),
        jsonl_path=str(tmp_path / "unused.jsonl"),
        store=str(store),
    )

    report_path = outputs / "populate_report.json"
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written == result
    assert written["store"] == str(store)
    assert written["instances"][0]["instance_id"] == "local900"


def test_instance_ids_in_the_store_are_a_subset_of_the_split(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs" / "train"
    _write_output(outputs, "local900")
    split = _write_split(tmp_path / "train.txt", ["local900", "local901"])
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    def fake_populate(**kwargs):
        from tkboost import TKStoreEntry

        TKStore(kwargs["store"]).insert(
            TKStoreEntry(
                instance_id=kwargs["instance_id"],
                db="TestDB",
                scope="db",
                sql_operations="select",
                rule="a rule",
            )
        )
        return {
            "instance_id": kwargs["instance_id"],
            "rule_count": 1,
            "skipped": None,
        }

    monkeypatch.setattr("tkstore.populate.populate_from_output_dir", fake_populate)
    populate_split(
        outputs_base=str(outputs),
        split_file=str(split),
        jsonl_path=str(tmp_path / "unused.jsonl"),
        store=str(store),
    )

    ids = {r["instance_id"] for r in TKStore(str(store)).rows()}
    assert ids <= {"local900", "local901"}
    assert ids == {"local900"}
    assert len(TKStore(str(store)).rows()) > 0


def test_cli_exits_nonzero_on_a_split_leak(tmp_path, stub_single):
    outputs = tmp_path / "outputs" / "train"
    _write_output(outputs, "local900")
    _write_output(outputs, "local999")
    split = _write_split(tmp_path / "train.txt", ["local900"])
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    from tkstore.populate import main

    code = main(
        [
            "--outputs-base",
            str(outputs),
            "--split-file",
            str(split),
            "--jsonl-path",
            str(tmp_path / "unused.jsonl"),
            "--store",
            str(store),
        ]
    )

    assert code == 1
    assert stub_single == []


def test_cli_defaults_the_store_to_artifacts(tmp_path, stub_single):
    from tkstore.populate import build_parser

    args = build_parser().parse_args(
        ["--outputs-base", "outputs/train", "--split-file", "data/splits/spider2_sqlite_train.txt"]
    )

    assert args.store == "artifacts/tkstore_sqlite.csv"
    assert args.jsonl_path == "data/spider2-lite.jsonl"
