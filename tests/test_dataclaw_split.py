"""Stratified train/test split over DataClaw tasks."""

from collections import Counter

import pytest

from tkstore.dataclaw.split import split_tasks, stratified_halves, task_strata


def _strata(sizes: dict[tuple[str, str], int]) -> dict[str, tuple[str, str]]:
    strata = {}
    for (category, level), n in sizes.items():
        for i in range(n):
            strata[f"task_{category}_{level}_{i:03d}"] = (category, level)
    return strata


SIZES = {("a", "easy"): 6, ("a", "hard"): 19, ("b", "medium"): 45, ("c", "easy"): 11, ("c", "hard"): 4}
STRATA = _strata(SIZES)


def _write_task(tasks_dir, task_id, name, category):
    (tasks_dir / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\nname: {name}\ncategory: {category}\n"
        "grading_type: llm_judge\n---\n## Prompt\nq\n",
        encoding="utf-8",
    )


class TestTaskStrata:
    def test_reads_category_and_level_from_frontmatter(self, tmp_path):
        _write_task(tmp_path, "task_001_x", "comprehensive_decision-easy-easy001", "comprehensive_decision")
        _write_task(tmp_path, "task_002_y", "risk_assessment-hard-hard004", "risk_assessment")

        assert task_strata(tmp_path) == {
            "task_001_x": ("comprehensive_decision", "easy"),
            "task_002_y": ("risk_assessment", "hard"),
        }

    def test_unknown_level_is_rejected(self, tmp_path):
        _write_task(tmp_path, "task_001_x", "comprehensive_decision-extreme-x001", "comprehensive_decision")

        with pytest.raises(ValueError):
            task_strata(tmp_path)

    def test_name_whose_category_disagrees_is_rejected(self, tmp_path):
        _write_task(tmp_path, "task_001_x", "risk_assessment-easy-easy001", "comprehensive_decision")

        with pytest.raises(ValueError):
            task_strata(tmp_path)


class TestStratifiedHalves:
    def test_halves_are_disjoint_and_cover_every_task(self):
        first, second = stratified_halves(STRATA, seed=0)

        assert set(first).isdisjoint(second)
        assert set(first) | set(second) == set(STRATA)

    def test_every_stratum_is_split_within_one(self):
        first, second = stratified_halves(STRATA, seed=0)

        in_first = Counter(STRATA[t] for t in first)
        in_second = Counter(STRATA[t] for t in second)
        for stratum, n in SIZES.items():
            assert abs(in_first[stratum] - in_second[stratum]) <= 1
            assert in_first[stratum] + in_second[stratum] == n

    def test_odd_remainders_alternate_so_totals_stay_balanced(self):
        """Three odd strata: always giving the extra task to one side would make it 3 larger."""
        first, second = stratified_halves(STRATA, seed=0)

        assert abs(len(first) - len(second)) <= 1

    def test_same_seed_reproduces_the_split(self):
        assert stratified_halves(STRATA, seed=3) == stratified_halves(STRATA, seed=3)

    def test_different_seed_changes_the_split(self):
        assert stratified_halves(STRATA, seed=0) != stratified_halves(STRATA, seed=1)

    def test_input_order_does_not_affect_the_split(self):
        reversed_strata = dict(reversed(list(STRATA.items())))

        assert stratified_halves(reversed_strata, seed=0) == stratified_halves(STRATA, seed=0)

    def test_outputs_are_sorted(self):
        first, second = stratified_halves(STRATA, seed=0)

        assert first == sorted(first)
        assert second == sorted(second)


class TestSplitTasks:
    def test_test_and_both_train_halves_partition_the_tasks(self):
        split = split_tasks(STRATA, seed=0)

        parts = [set(split.test), set(split.train_a), set(split.train_b)]
        assert sum(len(p) for p in parts) == len(STRATA)
        assert set().union(*parts) == set(STRATA)

    def test_test_is_half_and_each_train_half_is_a_quarter(self):
        split = split_tasks(STRATA, seed=0)

        assert abs(len(split.test) - len(STRATA) / 2) <= 1
        assert abs(len(split.train_a) - len(split.train_b)) <= 1

    def test_train_halves_are_stratified_within_train(self):
        split = split_tasks(STRATA, seed=0)

        a = Counter(STRATA[t] for t in split.train_a)
        b = Counter(STRATA[t] for t in split.train_b)
        for stratum in SIZES:
            assert abs(a[stratum] - b[stratum]) <= 1

    def test_train_is_the_union_of_both_halves(self):
        split = split_tasks(STRATA, seed=0)

        assert split.train == sorted(split.train_a + split.train_b)
