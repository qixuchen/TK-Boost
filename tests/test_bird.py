"""BIRD mini-dev instance ids, splits, and gold-eval helpers."""

import json
from pathlib import Path

import pytest

from src.utils.bird import (
    TRAIN_FRACTION,
    TRAIN_SEED,
    assign_minidev_ids,
    dedupe_exact_rows,
    eval_standard_record,
    has_outermost_order_by,
    spider2_shaped_instance,
    write_bird_jsonl,
)
from src.utils.splits import make_split
from tkstore.populate import _load_instance_record


def _row(**overrides):
    base = {
        "question_id": 1,
        "db_id": "financial",
        "question": "how many?",
        "evidence": "",
        "SQL": "SELECT 1",
        "difficulty": "simple",
    }
    base.update(overrides)
    return base


class TestDedupeExactRows:
    def test_keeps_the_first_of_an_identical_pair(self):
        first = _row(question_id=137)
        duplicate = _row(question_id=137)
        other = _row(question_id=138, SQL="SELECT 2")

        kept = dedupe_exact_rows([first, other, duplicate])

        assert kept == [first, other]

    def test_does_not_drop_rows_that_differ_in_one_field(self):
        a = _row(question="q1")
        b = _row(question="q2")

        assert dedupe_exact_rows([a, b]) == [a, b]


class TestAssignMinidevIds:
    def test_ids_follow_json_order_with_zero_padding(self):
        records = assign_minidev_ids([_row(question_id=9), _row(question_id=3)])

        assert [r["instance_id"] for r in records] == ["minidev0000", "minidev0001"]
        assert records[0]["question_id"] == 9

    def test_does_not_mutate_the_input_dicts(self):
        original = _row()
        assign_minidev_ids([original])

        assert "instance_id" not in original


class TestHasOutermostOrderBy:
    def test_plain_order_by_counts(self):
        assert has_outermost_order_by("SELECT a FROM t ORDER BY a") is True

    def test_no_order_by_is_false(self):
        assert has_outermost_order_by("SELECT a FROM t") is False

    def test_subquery_order_by_does_not_count(self):
        sql = "SELECT a FROM (SELECT b FROM t ORDER BY b) AS s"
        assert has_outermost_order_by(sql) is False

    def test_window_order_by_does_not_count(self):
        sql = "SELECT SUM(x) OVER (ORDER BY y) FROM t"
        assert has_outermost_order_by(sql) is False

    def test_cte_internal_order_by_does_not_count(self):
        sql = "WITH cte AS (SELECT a FROM t ORDER BY a) SELECT * FROM cte"
        assert has_outermost_order_by(sql) is False

    def test_order_by_after_cte_counts(self):
        sql = "WITH cte AS (SELECT a FROM t) SELECT * FROM cte ORDER BY a"
        assert has_outermost_order_by(sql) is True

    def test_order_by_only_in_a_line_comment_does_not_count(self):
        assert has_outermost_order_by("SELECT a FROM t -- ORDER BY a") is False

    def test_order_by_only_in_a_string_literal_does_not_count(self):
        assert has_outermost_order_by("SELECT 'ORDER BY a' FROM t") is False


class TestEvalStandardRecord:
    def test_ignore_order_is_false_when_outermost_order_by_is_present(self):
        record = eval_standard_record("minidev0000", "SELECT a FROM t ORDER BY a")

        assert record == {
            "instance_id": "minidev0000",
            "condition_cols": [],
            "ignore_order": False,
        }

    def test_ignore_order_is_true_when_only_inner_order_by_is_present(self):
        sql = "SELECT a FROM (SELECT b FROM t ORDER BY b) AS s"
        record = eval_standard_record("minidev0001", sql)

        assert record["ignore_order"] is True


class TestSplitSizes:
    def test_seed_zero_quarter_split_is_124_and_374_on_498_ids(self):
        ids = [f"minidev{i:04d}" for i in range(498)]
        train_size = round(len(ids) * TRAIN_FRACTION)
        train, test = make_split(ids, train_size=train_size, seed=TRAIN_SEED)

        assert TRAIN_FRACTION == 0.25
        assert TRAIN_SEED == 0
        assert train_size == 124
        assert len(train) == 124
        assert len(test) == 374

    def test_first_round_test_subsample_is_125(self):
        ids = [f"minidev{i:04d}" for i in range(498)]
        _, test = make_split(ids, train_size=124, seed=TRAIN_SEED)
        test_sub, _ = make_split(test, train_size=round(len(test) / 3), seed=TRAIN_SEED)

        assert len(test_sub) == 125
        assert set(test_sub).issubset(test)


BIRD_JSON = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "minidev"
    / "MINIDEV"
    / "mini_dev_sqlite.json"
)


@pytest.mark.skipif(not BIRD_JSON.is_file(), reason="data/minidev is not linked")
class TestRealMinidevFile:
    def test_deduped_count_is_498(self):
        records = json.loads(BIRD_JSON.read_text(encoding="utf-8"))
        kept = dedupe_exact_rows(records)
        assigned = assign_minidev_ids(kept)

        assert len(records) == 500
        assert len(kept) == 498
        assert assigned[-1]["instance_id"] == "minidev0497"


class TestSpider2ShapedInstance:
    def test_maps_bird_fields_and_omits_gold_sql(self):
        record = assign_minidev_ids([_row(evidence="Count EUR rows.")])[0]
        row = spider2_shaped_instance(record)

        assert row == {
            "instance_id": "minidev0000",
            "db": "financial",
            "question": "how many?",
            "external_knowledge": "Count EUR rows.",
        }
        assert "SQL" not in row
        assert "db_id" not in row

    def test_blank_evidence_becomes_null(self):
        record = assign_minidev_ids([_row(evidence="  ")])[0]
        row = spider2_shaped_instance(record)

        assert row["external_knowledge"] is None

    def test_written_jsonl_is_readable_by_populate(self, tmp_path):
        records = assign_minidev_ids([_row(evidence="Count EUR rows.")])
        path = write_bird_jsonl(tmp_path / "bird_minidev.jsonl", records)

        loaded = _load_instance_record(str(path), "minidev0000")

        assert loaded["db"] == "financial"
        assert loaded["external_knowledge"] == "Count EUR rows."
        assert "SQL" not in loaded
