"""Adopting the refiner's own corrected SQL, the way upstream `tkboost.sql()` does.

The refiner already emits `suggested_fix_sql` -- a complete corrected fragment -- and it
reaches disk in `refiner_<name>.json`, but this runner has never read it. Instead it
forwards the prose `suggested_fix` to the main agent, which rewrites from scratch. That
agent has never seen a single rule, and measured across the four rounds its rewrite
matches the refiner's suggestion with a similarity of only 0.23-0.47.

Upstream substitutes the refiner's SQL directly (`tkboost/__init__.py:648-654`), and the
gains reported in the README come from that path. The paper, however, specifies feedback
that the *agent* acts on (Alg 5 line 13 concatenates `f` into the context), so this flag
reproduces the upstream code rather than the paper, and must stay opt-in.

Two deliberate departures from upstream: the rebuilt SQL is executed before being
adopted (upstream substitutes unvalidated), and a failure is *not* handed back to the
agent -- mixing both adoption paths in one run would measure neither.
"""

import json

import pytest

from src.agents import sql_agent_runner as runner


TWO_CTE_SQL = """WITH customer_totals AS (
SELECT 1 AS x FROM t1
),
monthly_revenue AS (
SELECT 2 AS y FROM t2
)
SELECT * FROM monthly_revenue"""

FIXED_CTE = "WITH customer_totals AS (\nSELECT 9 AS x FROM t1 WHERE x IS NOT NULL\n)"


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


class FakeExecutor:
    """Executes anything except SQL carrying the word `broken`."""

    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []

    def execute(self, sql):
        self.calls.append(sql)
        if 'broken' in sql.lower():
            raise RuntimeError("no such column: broken")
        return ["x"], [(1,)]

    def close(self):
        pass


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: FakeExecutor(calls))
    monkeypatch.setattr(runner, "write_csv", lambda *_a, **_k: None)
    return calls


@pytest.fixture
def no_agent(monkeypatch):
    """The whole point of the flag is that the main agent is not consulted."""
    monkeypatch.setattr(
        runner, "llm_completion",
        lambda **_k: pytest.fail("the agent must not be asked to rewrite under adoption"),
    )


def _verdict(status, sql=""):
    return {"status": status, "issues": ["wrong grain"], "suggested_fix": "add a filter",
            "suggested_fix_sql": sql, "tests": []}


def _refine(tmp_path, verdicts, **kwargs):
    """Run refinement, feeding `verdicts` to successive refiner calls."""
    seq = list(verdicts)

    def fake_refiner(**_k):
        return seq.pop(0) if seq else {"status": "ok", "issues": []}

    import unittest.mock as mock
    with mock.patch.object(runner, "refiner_run", fake_refiner):
        return runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb", question="q"),
            final_sql=TWO_CTE_SQL,
            predicted_cte_hint=None,
            engine="sqlite",
            db_path_or_cred=str(tmp_path / "x.sqlite"),
            messages=[],
            out_dir=tmp_path,
            model="gpt-4.1",
            verbose=False,
            **kwargs,
        )


class TestParserAndOptions:
    def test_adoption_is_off_by_default(self):
        """All four recorded rounds ran with the agent in the loop."""
        assert _args(["--instance-id", "local001"]).adopt_refiner_sql is False

    def test_the_flag_is_accepted(self):
        assert _args(["--instance-id", "local001", "--adopt-refiner-sql"]).adopt_refiner_sql is True

    def test_the_flag_travels_to_the_refinement_call(self):
        options = runner._refiner_options(
            _args(["--instance-id", "local001", "--adopt-refiner-sql"])
        )

        assert options["adopt_refiner_sql"] is True


class TestAdoptingACteFix:
    def test_the_refiners_sql_replaces_the_cte_body(self, tmp_path, executed, no_agent):
        final_sql, _ = _refine(
            tmp_path, [_verdict("issues", FIXED_CTE)], adopt_refiner_sql=True
        )

        assert "WHERE x IS NOT NULL" in final_sql
        assert "monthly_revenue" in final_sql, "the other CTE must survive"
        assert "SELECT * FROM monthly_revenue" in final_sql, "the remainder must survive"

    def test_only_the_first_cte_of_the_suggestion_is_taken(self, tmp_path, executed, no_agent):
        """The refiner is given one CTE but rewrites its context too in 35% of fragments,
        so extra CTEs in the suggestion are dropped -- same as upstream's `fixed_ctes[0]`."""
        two = ("WITH customer_totals AS (\nSELECT 9 AS x FROM t1\n),\n"
               "smuggled AS (\nSELECT 0 AS z FROM t9\n)\nSELECT * FROM smuggled")

        final_sql, _ = _refine(tmp_path, [_verdict("issues", two)], adopt_refiner_sql=True)

        assert "SELECT 9 AS x" in final_sql
        assert "smuggled" not in final_sql

    def test_an_adopted_fix_writes_the_artifacts_the_report_reads(self, tmp_path, executed, no_agent):
        """`_choose_and_mark_final_artifacts` and `rules_used` both key off these names."""
        _refine(tmp_path, [_verdict("issues", FIXED_CTE)], adopt_refiner_sql=True)

        assert (tmp_path / "execution_query_after_customer_totals.sql").is_file()

    def test_a_suggestion_that_does_not_run_is_not_adopted(self, tmp_path, executed, no_agent):
        """Upstream substitutes without executing; keeping the check is a deliberate
        improvement, since an unrunnable body would poison every later fragment."""
        final_sql, _ = _refine(
            tmp_path,
            [_verdict("issues", "WITH customer_totals AS (\nSELECT broken FROM t1\n)")],
            adopt_refiner_sql=True,
        )

        assert "broken" not in final_sql
        assert "SELECT 1 AS x" in final_sql, "the original body must be kept"

    def test_an_issues_verdict_without_sql_changes_nothing(self, tmp_path, executed, no_agent):
        final_sql, _ = _refine(tmp_path, [_verdict("issues", "")], adopt_refiner_sql=True)

        assert final_sql.strip() == TWO_CTE_SQL.strip()

    def test_an_ok_verdict_changes_nothing(self, tmp_path, executed, no_agent):
        final_sql, _ = _refine(
            tmp_path, [_verdict("ok", FIXED_CTE)], adopt_refiner_sql=True
        )

        assert final_sql.strip() == TWO_CTE_SQL.strip()


class TestAdoptingTheFinalSelect:
    def test_the_whole_query_is_replaced(self, tmp_path, executed, no_agent):
        """At the final-select stage the refiner sees the complete query, so its
        suggestion is the complete query."""
        whole = "WITH a AS (\nSELECT 1 AS x FROM t1\n)\nSELECT x FROM a ORDER BY x"

        final_sql, _ = _refine(
            tmp_path,
            [_verdict("ok"), _verdict("ok"), _verdict("issues", whole)],
            adopt_refiner_sql=True,
        )

        assert final_sql.strip() == whole.strip()
        assert (tmp_path / "execution_query_after_final_select.sql").is_file()


class TestTheDefaultPathIsUntouched:
    def test_without_the_flag_the_agent_still_rewrites(self, tmp_path, executed, monkeypatch):
        """The four recorded rounds must stay reproducible."""
        asked = []

        def fake_llm(**kwargs):
            asked.append(kwargs)
            return {"choices": [{"message": {"content": "<solution>SELECT 7 AS x</solution>"}}]}

        monkeypatch.setattr(runner, "llm_completion", fake_llm)
        monkeypatch.setattr(runner, "detect_sql_blocks", lambda _c: [])
        monkeypatch.setattr(runner, "detect_solution", lambda _c: "SELECT 7 AS x")

        _refine(tmp_path, [_verdict("issues", FIXED_CTE)])

        assert asked, "the agent should have been asked to rewrite"

    def test_the_report_records_which_path_ran(self, tmp_path, executed, no_agent):
        """Two arms differing only by adoption produce otherwise identical artifacts."""
        _refine(
            tmp_path, [_verdict("issues", FIXED_CTE)],
            adopt_refiner_sql=True, tkstore_path=str(tmp_path / "store.csv"),
        )

        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        assert report["adopt_refiner_sql"] is True
