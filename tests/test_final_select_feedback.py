"""The final-SELECT verdict must reach the agent, like every per-CTE verdict does.

Algorithm 5 concatenates every feedback `f` back into the agent context. Computing a
verdict for the final SELECT and discarding it means any query that parses into zero
CTEs can never be improved, however good the retrieved knowledge is.
"""

from pathlib import Path

import pytest

from src.agents import sql_agent_runner as runner


ONE_CTE_SQL = """WITH customer_totals AS (
SELECT 1 AS x FROM t1
)
SELECT * FROM customer_totals"""

NO_CTE_SQL = "SELECT order_id FROM orders WHERE status = 'delivered'"

REVISED_SQL = "SELECT order_id FROM orders WHERE lower(status) = 'delivered'"


class FakeExecutor:
    def execute(self, sql):
        return ["order_id"], [(1,)]

    def close(self):
        pass


class FailingExecutor:
    def execute(self, sql):
        raise RuntimeError("no such column: lower_status")

    def close(self):
        pass


@pytest.fixture
def refiner_flags_the_final_select(monkeypatch):
    """Every CTE is fine; only the final SELECT has issues."""
    calls = []

    def fake_refiner_run(**kwargs):
        calls.append(kwargs)
        if kwargs["cte_goal"].startswith("Final SELECT"):
            return {"status": "issues", "issues": ["status is stored capitalised"],
                    "suggested_fix": "use lower(status)"}
        return {"status": "ok", "issues": []}

    monkeypatch.setattr(runner, "refiner_run", fake_refiner_run)
    monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: FakeExecutor())
    return calls


def _run(out_dir: Path, final_sql: str, messages: list = None) -> tuple:
    inst = runner.Instance(instance_id="local999", db="testdb", question="q")
    return runner.perform_refinement_and_revision(
        inst=inst,
        final_sql=final_sql,
        predicted_cte_hint=None,
        engine="sqlite",
        db_path_or_cred=None,
        messages=messages if messages is not None else [],
        out_dir=out_dir,
        model="gpt-4.1",
        verbose=False,
    )


class TestFinalSelectRevision:
    def test_feedback_is_appended_to_the_agent_context(self, tmp_path, monkeypatch, refiner_flags_the_final_select):
        monkeypatch.setattr(runner, "llm_completion", lambda **_k: {"choices": [{"message": {
            "content": f"<solution>\n{REVISED_SQL}\n</solution>"}}]})
        messages = []

        _run(tmp_path, NO_CTE_SQL, messages)

        feedback = [m for m in messages if m["role"] == "user"]
        assert feedback, "the refiner verdict never reached the agent"
        assert "status is stored capitalised" in feedback[0]["content"]
        assert "use lower(status)" in feedback[0]["content"]

    def test_revised_sql_is_adopted_and_returned(self, tmp_path, monkeypatch, refiner_flags_the_final_select):
        monkeypatch.setattr(runner, "llm_completion", lambda **_k: {"choices": [{"message": {
            "content": f"<solution>\n{REVISED_SQL}\n</solution>"}}]})

        final_sql, _ = _run(tmp_path, NO_CTE_SQL)

        assert final_sql == REVISED_SQL

    def test_revision_is_persisted(self, tmp_path, monkeypatch, refiner_flags_the_final_select):
        monkeypatch.setattr(runner, "llm_completion", lambda **_k: {"choices": [{"message": {
            "content": f"<solution>\n{REVISED_SQL}\n</solution>"}}]})

        _run(tmp_path, NO_CTE_SQL)

        assert (tmp_path / "execution_query_after_final_select.sql").read_text(
            encoding="utf-8") == REVISED_SQL
        assert (tmp_path / "execution_result_after_final_select.csv").exists()

    def test_a_query_with_ctes_also_gets_the_final_select_revision(
        self, tmp_path, monkeypatch, refiner_flags_the_final_select
    ):
        monkeypatch.setattr(runner, "llm_completion", lambda **_k: {"choices": [{"message": {
            "content": f"<solution>\n{REVISED_SQL}\n</solution>"}}]})

        final_sql, _ = _run(tmp_path, ONE_CTE_SQL)

        assert final_sql == REVISED_SQL

    def test_nothing_happens_when_the_final_select_is_fine(self, tmp_path, monkeypatch):
        def fake_refiner_run(**_kwargs):
            return {"status": "ok", "issues": []}

        monkeypatch.setattr(runner, "refiner_run", fake_refiner_run)
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: FakeExecutor())
        monkeypatch.setattr(runner, "llm_completion",
                            lambda **_k: pytest.fail("no revision expected for an ok verdict"))

        final_sql, _ = _run(tmp_path, NO_CTE_SQL)

        assert final_sql == NO_CTE_SQL
        assert not (tmp_path / "execution_query_after_final_select.sql").exists()

    def test_an_unexecutable_revision_is_not_adopted(self, tmp_path, monkeypatch, refiner_flags_the_final_select):
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: FailingExecutor())
        monkeypatch.setattr(runner, "llm_completion", lambda **_k: {"choices": [{"message": {
            "content": f"<solution>\n{REVISED_SQL}\n</solution>"}}]})

        final_sql, _ = _run(tmp_path, NO_CTE_SQL)

        assert final_sql == NO_CTE_SQL
        assert not (tmp_path / "execution_query_after_final_select.sql").exists()


class TestFinalArtifactPrecedence:
    """The final SELECT is the last refinement stage, so its revision is the newest."""

    def test_final_select_revision_beats_the_last_cte_revision(self, tmp_path):
        (tmp_path / "execution_query.sql").write_text("original", encoding="utf-8")
        (tmp_path / "execution_query_after_customer_totals.sql").write_text("per cte", encoding="utf-8")
        (tmp_path / "execution_result_after_customer_totals.csv").write_text("a\n1\n", encoding="utf-8")
        (tmp_path / "execution_query_after_final_select.sql").write_text("final select", encoding="utf-8")
        (tmp_path / "execution_result_after_final_select.csv").write_text("b\n2\n", encoding="utf-8")

        runner._choose_and_mark_final_artifacts(tmp_path, last_cte_name="customer_totals")

        assert (tmp_path / "execution_query_final.sql").read_text(encoding="utf-8") == "final select"
        assert (tmp_path / "execution_result_final.csv").read_text(encoding="utf-8") == "b\n2\n"

    def test_last_cte_revision_still_wins_when_there_is_no_final_select_one(self, tmp_path):
        (tmp_path / "execution_query.sql").write_text("original", encoding="utf-8")
        (tmp_path / "execution_query_after_customer_totals.sql").write_text("per cte", encoding="utf-8")

        runner._choose_and_mark_final_artifacts(tmp_path, last_cte_name="customer_totals")

        assert (tmp_path / "execution_query_final.sql").read_text(encoding="utf-8") == "per cte"
