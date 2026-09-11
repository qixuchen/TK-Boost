"""Failure handling in the Spider2 evaluator.

Deviation C5: when nothing matched, the evaluator raised `KeyError: 'score'` from
an empty DataFrame instead of explaining what went wrong. The two ways to end up
with nothing were a trailing slash on `--result_dir` (which made
`os.path.basename` return an empty string, so the instance id never parsed) and a
prediction CSV that was empty because the agent's SQL failed to execute.
"""

import pytest

from evaluation.evaluate import (
    NoPredictionsFound,
    build_eval_dataframe,
    normalize_result_dir,
    parse_instance_id_from_output_dir,
    require_predictions,
)

EXPECTED_COLUMNS = ["instance_id", "score", "score_final", "assistant_turns"]


class TestBuildEvalDataframe:
    def test_empty_input_still_has_the_score_columns(self):
        """The empty case must not produce a column-less frame."""
        df = build_eval_dataframe([])

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 0

    def test_empty_input_allows_score_column_access(self):
        df = build_eval_dataframe([])

        assert df["score"].sum() == 0
        assert df["score_final"].sum() == 0

    def test_missing_score_keys_default_to_zero(self):
        df = build_eval_dataframe([{"instance_id": "local001"}])

        assert df.loc[0, "score"] == 0
        assert df.loc[0, "score_final"] == 0

    def test_none_scores_are_treated_as_zero(self):
        df = build_eval_dataframe([{"instance_id": "local001", "score": None}])

        assert df.loc[0, "score"] == 0

    def test_supplied_scores_are_preserved(self):
        df = build_eval_dataframe(
            [{"instance_id": "local001", "score": 1, "score_final": 1, "assistant_turns": 7}]
        )

        assert df.loc[0, "score"] == 1
        assert df.loc[0, "score_final"] == 1
        assert df.loc[0, "assistant_turns"] == 7

    def test_extra_keys_are_dropped(self):
        df = build_eval_dataframe(
            [{"instance_id": "local001", "score": 0, "pred_sql": "SELECT 1", "error_info": "x"}]
        )

        assert list(df.columns) == EXPECTED_COLUMNS


class TestNormalizeResultDir:
    @pytest.mark.parametrize(
        "given",
        [
            "outputs/local007_20260910_172039",
            "outputs/local007_20260910_172039/",
            "outputs/local007_20260910_172039//",
        ],
    )
    def test_trailing_slashes_do_not_break_instance_id_parsing(self, given):
        import os

        normalized = normalize_result_dir(given)

        assert parse_instance_id_from_output_dir(os.path.basename(normalized)) == "local007"

    def test_plain_path_is_unchanged(self):
        assert normalize_result_dir("outputs") == "outputs"

    def test_root_is_preserved(self):
        assert normalize_result_dir("/") == "/"

    def test_empty_string_is_preserved(self):
        assert normalize_result_dir("") == ""


class TestRequirePredictions:
    def test_passes_when_predictions_exist(self):
        require_predictions(["local007"], [], "outputs")

    def test_passes_when_only_missing_csv_instances_exist(self):
        """Instances whose SQL failed still count as evaluable; they score 0."""
        require_predictions([], ["local007"], "outputs")

    def test_raises_when_nothing_matched(self):
        with pytest.raises(NoPredictionsFound):
            require_predictions([], [], "outputs")

    def test_error_names_the_directory_and_the_likely_causes(self):
        with pytest.raises(NoPredictionsFound) as excinfo:
            require_predictions([], [], "outputs/typo")

        message = str(excinfo.value)
        assert "outputs/typo" in message
        assert "_YYYYMMDD_HHMMSS" in message
