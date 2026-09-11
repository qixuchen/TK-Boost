"""The correctness gate that Algorithm 3 needs.

Populate may only learn from instances the agent got wrong, so it asks the same
question the final evaluation asks, through the same comparison, rather than
inventing a second notion of "correct".
"""

import json

import pandas as pd
import pytest

from evaluation.evaluate import agent_result_matches_gold


@pytest.fixture
def gold_dir(tmp_path):
    """A minimal `evaluation/gold` tree with one single-variant instance."""
    root = tmp_path / "gold"
    (root / "exec_result").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "n": [1, 2]}).to_csv(
        root / "exec_result" / "local001.csv", index=False
    )
    (root / "spider2lite_eval.jsonl").write_text(
        json.dumps({"instance_id": "local001", "condition_cols": [], "ignore_order": False}) + "\n",
        encoding="utf-8",
    )
    return root


def _write_pred(tmp_path, frame):
    path = tmp_path / "execution_result.csv"
    frame.to_csv(path, index=False)
    return path


def test_matching_result_is_correct(tmp_path, gold_dir):
    pred = _write_pred(tmp_path, pd.DataFrame({"name": ["a", "b"], "n": [1, 2]}))

    assert agent_result_matches_gold(str(pred), "local001", str(gold_dir)) is True


def test_different_values_are_incorrect(tmp_path, gold_dir):
    pred = _write_pred(tmp_path, pd.DataFrame({"name": ["a", "b"], "n": [1, 99]}))

    assert agent_result_matches_gold(str(pred), "local001", str(gold_dir)) is False


def test_empty_prediction_file_is_incorrect_not_an_error(tmp_path, gold_dir):
    """A failed agent SQL leaves a zero-byte CSV; that is the strongest
    correction signal, so it must be reported as incorrect rather than raise."""
    pred = tmp_path / "execution_result.csv"
    pred.write_text("", encoding="utf-8")

    assert agent_result_matches_gold(str(pred), "local001", str(gold_dir)) is False


def test_missing_prediction_file_is_incorrect(tmp_path, gold_dir):
    missing = tmp_path / "nope" / "execution_result.csv"

    assert agent_result_matches_gold(str(missing), "local001", str(gold_dir)) is False


def test_matching_any_gold_variant_is_correct(tmp_path, gold_dir):
    pd.DataFrame({"name": ["a", "b"], "n": [1, 2]}).to_csv(
        gold_dir / "exec_result" / "local002_a.csv", index=False
    )
    pd.DataFrame({"name": ["x", "y"], "n": [7, 8]}).to_csv(
        gold_dir / "exec_result" / "local002_b.csv", index=False
    )
    with open(gold_dir / "spider2lite_eval.jsonl", "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"instance_id": "local002", "condition_cols": [], "ignore_order": False}) + "\n"
        )
    pred = _write_pred(tmp_path, pd.DataFrame({"name": ["x", "y"], "n": [7, 8]}))

    assert agent_result_matches_gold(str(pred), "local002", str(gold_dir)) is True


def test_missing_gold_result_fails_closed(tmp_path, gold_dir):
    """Silently treating an unjudgeable instance as incorrect would feed
    Algorithm 3 a correction it cannot ground."""
    pred = _write_pred(tmp_path, pd.DataFrame({"name": ["a"], "n": [1]}))

    with pytest.raises(FileNotFoundError):
        agent_result_matches_gold(str(pred), "local999", str(gold_dir))


def test_instance_absent_from_eval_standard_fails_closed(tmp_path, gold_dir):
    pd.DataFrame({"name": ["a"], "n": [1]}).to_csv(
        gold_dir / "exec_result" / "local003.csv", index=False
    )
    pred = _write_pred(tmp_path, pd.DataFrame({"name": ["a"], "n": [1]}))

    with pytest.raises(KeyError):
        agent_result_matches_gold(str(pred), "local003", str(gold_dir))
