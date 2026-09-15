"""Turning three arms of output into the stage 4 comparison table.

`delta_knowledge` (arm B - arm A) is the number that can be attributed to tribal
knowledge, because both arms carry the refiner and differ only by the store.
`delta_paper` (arm B - bare agent) matches the paper's Fig. 6 but also carries the
refiner's own gain.
"""

import json

import pytest

from src.utils import compare


def _evals(path, rows):
    """An `evals.csv` in the shape `evaluate.py` writes."""
    header = "instance_id,score,score_final,assistant_turns\n"
    body = "".join(f"{i},{s},{sf},{t}\n" for i, s, sf, t in rows)
    path.write_text(header + body, encoding="utf-8")
    return path


def _instance(base, instance_id, retrievals=None, verdicts=None, after=()):
    d = base / f"{instance_id}_20260101_000000"
    d.mkdir(parents=True)
    (d / "execution_query.sql").write_text("SELECT 1", encoding="utf-8")
    if retrievals is not None:
        (d / "retrieved_rules.json").write_text(
            json.dumps({"n_ctes": 1, "retrievals": retrievals}), encoding="utf-8"
        )
    for name, status in (verdicts or {}).items():
        (d / f"refiner_{name}.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    for name in after:
        (d / f"execution_query_after_{name}.sql").write_text("SELECT 2", encoding="utf-8")
    return d


def _cte(name, selected):
    return {"stage": "cte", "name": name, "sql_sha1": "x", "candidates": selected, "selected": selected}


def _final(selected):
    return {"stage": "final_select", "name": "_final_select", "sql_sha1": "x",
            "candidates": selected, "selected": selected}


class TestLoadScores:
    def test_scores_are_keyed_by_instance(self, tmp_path):
        path = _evals(tmp_path / "evals.csv", [("local001", 1, 0, 12), ("local002", 0, 1, 8)])

        scores = compare.load_scores(path)

        assert scores["local001"] == {"score": 1, "score_final": 0}
        assert scores["local002"] == {"score": 0, "score_final": 1}

    def test_blank_scores_read_as_zero(self, tmp_path):
        """`evaluate.py` leaves the cell empty when there is no prediction CSV."""
        path = tmp_path / "evals.csv"
        path.write_text("instance_id,score,score_final,assistant_turns\nlocal001,,,\n", encoding="utf-8")

        assert compare.load_scores(path) == {"local001": {"score": 0, "score_final": 0}}

    def test_a_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="evals.csv"):
            compare.load_scores(tmp_path / "evals.csv")


class TestRulesUsed:
    """The upper bound on rules that could have changed the output, not proof they did."""

    def test_an_adopted_rewrite_contributes_its_rules(self, tmp_path):
        d = _instance(tmp_path, "local001", retrievals=[_cte("a", ["7", "42"])],
                      verdicts={"a": "issues"}, after=["a"])

        assert compare.rules_used(d) == ["7", "42"]

    def test_an_ok_verdict_contributes_nothing(self, tmp_path):
        """Rules the refiner absorbed silently never reached the agent."""
        d = _instance(tmp_path, "local001", retrievals=[_cte("a", ["7"])], verdicts={"a": "ok"})

        assert compare.rules_used(d) == []

    def test_issues_without_an_adopted_rewrite_contributes_nothing(self, tmp_path):
        """The revision may have been unexecutable, so the SQL never changed."""
        d = _instance(tmp_path, "local001", retrievals=[_cte("a", ["7"])], verdicts={"a": "issues"})

        assert compare.rules_used(d) == []

    def test_the_final_select_stage_maps_to_its_own_artifacts(self, tmp_path):
        """Its retrieval is named `_final_select` but its files are `*_final_select.*`."""
        d = _instance(tmp_path, "local001", retrievals=[_final(["3"])],
                      verdicts={"final_select": "issues"}, after=["final_select"])

        assert compare.rules_used(d) == ["3"]

    def test_rules_are_deduplicated_across_fragments(self, tmp_path):
        d = _instance(tmp_path, "local001",
                      retrievals=[_cte("a", ["7", "9"]), _cte("b", ["9", "10"])],
                      verdicts={"a": "issues", "b": "issues"}, after=["a", "b"])

        assert compare.rules_used(d) == ["7", "9", "10"]

    def test_an_arm_without_a_store_has_no_rules(self, tmp_path):
        """Arm A writes no `retrieved_rules.json`; that is expected, not an error."""
        d = _instance(tmp_path, "local001", verdicts={"a": "issues"}, after=["a"])

        assert compare.rules_used(d) == []


class TestDbRuleCounts:
    def test_only_db_scoped_rules_are_counted(self, tmp_path):
        store = tmp_path / "store.csv"
        store.write_text(
            "mem_id,instance_id,db,scope,rule\n"
            "0,local003,IPL,db,r\n"
            "1,local003,IPL,generic,r\n"
            "2,local019,f1,db,r\n",
            encoding="utf-8",
        )

        assert compare.db_rule_counts(store) == {"ipl": 1, "f1": 1}

    def test_database_names_are_matched_case_insensitively(self, tmp_path):
        """The store spells one database both `AdventureWorks` and `adventureworks`;
        retrieval lowercases both sides, so counting must too."""
        store = tmp_path / "store.csv"
        store.write_text(
            "mem_id,instance_id,db,scope,rule\n"
            "0,local003,AdventureWorks,db,r\n"
            "1,local004,adventureworks,db,r\n",
            encoding="utf-8",
        )

        assert compare.db_rule_counts(store) == {"adventureworks": 2}


class TestCompareArms:
    @pytest.fixture
    def arms(self, tmp_path):
        a, b = tmp_path / "refonly", tmp_path / "tk"
        a.mkdir()
        b.mkdir()
        return a, b

    @pytest.fixture
    def store(self, tmp_path):
        path = tmp_path / "store.csv"
        path.write_text("mem_id,instance_id,db,scope,rule\n0,local003,IPL,db,r\n", encoding="utf-8")
        return path

    def test_deltas_come_from_the_two_final_scores(self, arms, store):
        a, b = arms
        _evals(a / "evals.csv", [("local001", 0, 0, 1)])
        _evals(b / "evals.csv", [("local001", 0, 1, 1)])
        _instance(b, "local001", retrievals=[_cte("x", ["0"])], verdicts={"x": "issues"}, after=["x"])

        row = compare.compare_arms(a, b, store, {"local001": "IPL"}).rows[0]

        assert (row.score_bare, row.score_arm_a, row.score_arm_b) == (0, 0, 1)
        assert row.delta_knowledge == 1
        assert row.delta_paper == 1
        assert row.rules_used == ["0"]
        assert row.n_db_rules == 1

    def test_knowledge_making_it_worse_is_a_negative_delta(self, arms, store):
        a, b = arms
        _evals(a / "evals.csv", [("local001", 0, 1, 1)])
        _evals(b / "evals.csv", [("local001", 0, 0, 1)])

        row = compare.compare_arms(a, b, store, {"local001": "IPL"}).rows[0]

        assert row.delta_knowledge == -1

    def test_an_instance_without_db_rules_is_still_reported(self, arms, store):
        a, b = arms
        _evals(a / "evals.csv", [("local001", 1, 1, 1)])
        _evals(b / "evals.csv", [("local001", 1, 1, 1)])

        row = compare.compare_arms(a, b, store, {"local001": "city_legislation"}).rows[0]

        assert row.n_db_rules == 0

    def test_disagreeing_bare_scores_are_flagged(self, arms, store):
        """Both arms refine the same agent output, so `score` must match. If it does not,
        the arms did not share a starting query and no delta is attributable."""
        a, b = arms
        _evals(a / "evals.csv", [("local001", 1, 1, 1)])
        _evals(b / "evals.csv", [("local001", 0, 1, 1)])

        result = compare.compare_arms(a, b, store, {"local001": "IPL"})

        assert any("local001" in w and "bare" in w.lower() for w in result.warnings)
        assert result.rows[0].paired is False

    def test_agreeing_bare_scores_mark_the_row_paired(self, arms, store):
        a, b = arms
        _evals(a / "evals.csv", [("local001", 1, 1, 1)])
        _evals(b / "evals.csv", [("local001", 1, 1, 1)])

        assert compare.compare_arms(a, b, store, {"local001": "IPL"}).rows[0].paired is True

    def test_an_instance_missing_from_one_arm_is_excluded_and_flagged(self, arms, store):
        a, b = arms
        _evals(a / "evals.csv", [("local001", 1, 1, 1), ("local002", 1, 1, 1)])
        _evals(b / "evals.csv", [("local001", 1, 1, 1)])

        result = compare.compare_arms(a, b, store, {"local001": "IPL", "local002": "IPL"})

        assert [r.instance_id for r in result.rows] == ["local001"]
        assert any("local002" in w for w in result.warnings)

    def test_rows_come_out_in_instance_order(self, arms, store):
        a, b = arms
        _evals(a / "evals.csv", [("local002", 1, 1, 1), ("local001", 1, 1, 1)])
        _evals(b / "evals.csv", [("local002", 1, 1, 1), ("local001", 1, 1, 1)])

        rows = compare.compare_arms(a, b, store, {"local001": "IPL", "local002": "IPL"}).rows

        assert [r.instance_id for r in rows] == ["local001", "local002"]


def _row(instance_id, bare, arm_a, arm_b, n_db_rules, paired=True):
    return compare.Row(
        instance_id=instance_id, db="d", score_bare=bare, score_arm_a=arm_a, score_arm_b=arm_b,
        delta_knowledge=arm_b - arm_a, delta_paper=arm_b - bare, rules_used=[],
        n_db_rules=n_db_rules, paired=paired,
    )


class TestSummariseByDbRules:
    """Overall gain is dominated by a few databases, so the split has to be reported."""

    def test_instances_split_on_having_any_db_rule(self):
        rows = [_row("local001", 0, 0, 1, 0), _row("local002", 0, 0, 1, 12)]

        groups = {g.label: g for g in compare.summarise_by_db_rules(rows)}

        assert groups["no db rules"].n == 1
        assert groups["has db rules"].n == 1

    def test_a_group_counts_correct_instances_per_arm(self):
        rows = [_row("local001", 1, 1, 1, 3), _row("local002", 0, 0, 1, 3)]

        group = next(g for g in compare.summarise_by_db_rules(rows) if g.label == "has db rules")

        assert (group.bare, group.arm_a, group.arm_b) == (1, 1, 2)

    def test_a_group_counts_improvements_and_regressions_separately(self):
        """A net delta of zero can hide one fix and one break."""
        rows = [_row("local001", 0, 0, 1, 3), _row("local002", 0, 1, 0, 3)]

        group = next(g for g in compare.summarise_by_db_rules(rows) if g.label == "has db rules")

        assert (group.improved, group.regressed) == (1, 1)

    def test_the_overall_group_is_reported_too(self):
        rows = [_row("local001", 0, 0, 1, 0), _row("local002", 0, 0, 1, 12)]

        groups = {g.label: g for g in compare.summarise_by_db_rules(rows)}

        assert groups["overall"].n == 2
        assert groups["overall"].arm_b == 2

    def test_unpaired_rows_are_left_out_of_the_aggregate(self):
        """Their delta measures something other than knowledge, so averaging them in
        would corrupt the headline number behind a warning line."""
        rows = [_row("local001", 0, 0, 1, 3), _row("local002", 0, 0, 1, 3, paired=False)]

        group = next(g for g in compare.summarise_by_db_rules(rows) if g.label == "overall")

        assert group.n == 1
        assert group.improved == 1

    def test_the_number_of_excluded_rows_is_reported(self):
        rows = [_row("local001", 0, 0, 1, 3), _row("local002", 0, 0, 1, 3, paired=False)]

        group = next(g for g in compare.summarise_by_db_rules(rows) if g.label == "overall")

        assert group.unpaired == 1
