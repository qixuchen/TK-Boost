"""BIRD mini-dev loading and runner integration."""

import csv
import json
from pathlib import Path

import pytest

from src.agents import sql_agent_runner as runner
from src.utils.agent_utils import load_external_knowledge


def _bird_row(**overrides):
    row = {
        "question_id": 10,
        "db_id": "financial",
        "question": "How many accounts?",
        "evidence": "Count distinct account_id.",
        "SQL": "SELECT COUNT(DISTINCT account_id) FROM account",
        "difficulty": "simple",
    }
    row.update(overrides)
    return row


def test_bird_loader_deduplicates_and_maps_fields_without_gold_sql(tmp_path):
    first = _bird_row()
    duplicate = dict(first)
    second = _bird_row(
        question_id=11,
        db_id="card_games",
        question="Which cards?",
        evidence="",
        SQL="SELECT name FROM cards",
    )
    path = tmp_path / "mini_dev_sqlite.json"
    path.write_text(json.dumps([first, duplicate, second]), encoding="utf-8")

    instances = runner.load_instances_from_bird_json(str(path))

    assert [instance.instance_id for instance in instances] == [
        "minidev0000",
        "minidev0001",
    ]
    assert instances[0].db == "financial"
    assert instances[0].question == "How many accounts?"
    assert instances[0].external_knowledge == "Count distinct account_id."
    assert instances[1].external_knowledge is None
    assert "SQL" not in instances[0].__dict__


REPO_ROOT = Path(__file__).resolve().parent.parent
BIRD_JSON = REPO_ROOT / "data/minidev/MINIDEV/mini_dev_sqlite.json"
TRAIN_SPLIT = REPO_ROOT / "data/splits/bird_minidev_train.txt"
INDEX_CSV = REPO_ROOT / "data/splits/bird_minidev_index.csv"


@pytest.mark.skipif(not BIRD_JSON.is_file(), reason="data/minidev is not linked")
def test_real_bird_loader_matches_frozen_train_split_and_index():
    instances = runner.load_instances_from_bird_json(str(BIRD_JSON))
    by_id = {instance.instance_id: instance for instance in instances}
    train_ids = [
        line.strip()
        for line in TRAIN_SPLIT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    with INDEX_CSV.open(encoding="utf-8", newline="") as f:
        index = {row["instance_id"]: row for row in csv.DictReader(f)}

    assert len(instances) == 498
    assert len(train_ids) == 124
    assert set(train_ids).issubset(by_id)
    for instance_id in (train_ids[0], train_ids[-1]):
        assert by_id[instance_id].db == index[instance_id]["db_id"]


def test_minidev_external_knowledge_is_inline_evidence():
    evidence = "  Count EUR rows and divide by the number of CZK rows.  "

    assert (
        load_external_knowledge("minidev0000", evidence)
        == "Count EUR rows and divide by the number of CZK rows."
    )


def test_spider2_external_knowledge_still_loads_a_file(tmp_path, monkeypatch):
    knowledge_dir = tmp_path / "data/spider2/local003"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "RFM.md").write_text("RFM explanation", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert load_external_knowledge("local003", "RFM.md") == "RFM explanation"


def test_missing_spider2_external_knowledge_still_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert load_external_knowledge("local003", "missing.md") is None


def test_load_ground_truth_reads_an_explicit_gold_directory(tmp_path):
    gold_dir = tmp_path / "gold_bird"
    (gold_dir / "sql").mkdir(parents=True)
    (gold_dir / "exec_result").mkdir()
    (gold_dir / "sql/minidev0000.sql").write_text("SELECT 7\n", encoding="utf-8")
    (gold_dir / "exec_result/minidev0000.csv").write_text(
        "answer\n7\n", encoding="utf-8"
    )

    query, result, column_variants = runner.load_ground_truth(
        "minidev0000", gold_dir=gold_dir
    )

    assert query == "SELECT 7"
    assert result == [(7,)]
    assert column_variants == [["answer"]]


def test_bird_and_explicit_jsonl_paths_are_mutually_exclusive():
    parser = runner._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--instance-id",
                "minidev0000",
                "--bird-json",
                "bird.json",
                "--jsonl-path",
                "spider.jsonl",
            ]
        )


def test_bird_source_selects_bird_gold_by_default():
    args = runner._build_parser().parse_args(
        ["--instance-id", "minidev0000", "--bird-json", "bird.json"]
    )

    assert runner._gold_dir_for_args(args) == Path("evaluation/gold_bird")


def test_spider2_source_keeps_existing_gold_default():
    args = runner._build_parser().parse_args(["--instance-id", "local003"])

    assert runner._gold_dir_for_args(args) == Path("evaluation/gold")


def test_explicit_gold_directory_overrides_source_default(tmp_path):
    args = runner._build_parser().parse_args(
        [
            "--instance-id",
            "minidev0000",
            "--bird-json",
            "bird.json",
            "--gold-dir",
            str(tmp_path),
        ]
    )

    assert runner._gold_dir_for_args(args) == tmp_path


def test_load_instances_dispatches_to_bird_loader(monkeypatch):
    expected = [runner.Instance("minidev0000", "financial", "q", "e")]
    monkeypatch.setattr(
        runner, "load_instances_from_bird_json", lambda path: expected if path == "bird.json" else []
    )
    args = runner._build_parser().parse_args(
        ["--instance-id", "minidev0000", "--bird-json", "bird.json"]
    )

    assert runner.load_instances(args) == expected


def test_minidev_never_receives_gold_column_name_hint():
    assert (
        runner.expected_output_format_for_instance(
            "minidev0000", [["gold_only_name"]]
        )
        is None
    )


def test_spider2_keeps_existing_gold_column_name_hint():
    hint = runner.expected_output_format_for_instance(
        "local003", [["customer_id", "amount"]]
    )

    assert hint == (
        "Expected Output Format: columns=['customer_id', 'amount'] "
        "(use this exact order)."
    )


def test_dry_run_validates_without_calling_agent_or_creating_output(
    tmp_path, monkeypatch, capsys
):
    bird_json = tmp_path / "bird.json"
    bird_json.write_text(json.dumps([_bird_row()]), encoding="utf-8")
    split = tmp_path / "split.txt"
    split.write_text("minidev0000\n", encoding="utf-8")
    out_base = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        "sys.argv",
        [
            "sql_agent_runner",
            "--bird-json",
            str(bird_json),
            "--split",
            str(split),
            "--gold-dir",
            str(tmp_path / "gold"),
            "--out-base",
            str(out_base),
            "--dry-run",
        ],
    )
    monkeypatch.setattr(
        runner, "resolve_sqlite_db_path", lambda *_args: "/tmp/financial.sqlite"
    )
    monkeypatch.setattr(
        runner,
        "load_ground_truth",
        lambda *_args, **_kwargs: ("SELECT 1", [(1,)], [["answer"]]),
    )
    monkeypatch.setattr(
        runner,
        "run_agent",
        lambda **_kwargs: pytest.fail("dry-run must not call the LLM agent"),
    )

    runner.main()

    output = capsys.readouterr().out
    assert "minidev0000" in output
    assert "evidence_chars=26" in output
    assert "gold_csv_ok=True" in output
    assert "expected_output_format=None" in output
    assert not out_base.exists()
