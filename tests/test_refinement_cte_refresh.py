"""Per-CTE refinement must work off the latest adopted solution.

Algorithm 5 re-derives `c_i` and `number_of_CTEs` from the current `s_t` on every
iteration, so once the agent adopts a revised solution the remaining CTEs have to
come from that revision rather than from the snapshot taken before the loop.

The CTE names here are deliberately long: `parse_ctes_from_sql` decides where the
remainder starts from a 20-character lookahead, so short names make it stop after
the first CTE.
"""

from pathlib import Path

import pytest

from src.agents import sql_agent_runner as runner


ORIGINAL_SQL = """WITH customer_totals AS (
SELECT 1 AS x FROM t1
),
monthly_revenue AS (
SELECT 2 AS y FROM t2
)
SELECT * FROM monthly_revenue"""

# Same shape, but both CTE bodies changed. The agent is asked to touch only one
# CTE, yet it routinely rewrites more, which is exactly when staleness shows up.
REVISED_SQL = """WITH customer_totals AS (
SELECT 10 AS x FROM t1
),
monthly_revenue AS (
SELECT 20 AS y FROM t2
)
SELECT * FROM monthly_revenue"""

SINGLE_CTE_SQL = """WITH customer_totals AS (
SELECT 10 AS x FROM t1
)
SELECT * FROM customer_totals"""


class FakeExecutor:
    def execute(self, sql):
        return ["x"], [(1,)]

    def close(self):
        pass


def _cte_calls(calls: list) -> list:
    """Refiner calls for individual CTEs, excluding the final-SELECT pass."""
    return [c for c in calls if not c["cte_goal"].startswith("Final SELECT")]


@pytest.fixture
def refiner_calls(monkeypatch):
    """Record every refiner invocation; flag issues on the first CTE only."""
    calls = []

    def fake_refiner_run(**kwargs):
        calls.append(kwargs)
        if len(_cte_calls(calls)) == 1:
            return {"status": "issues", "issues": ["wrong filter"]}
        return {"status": "ok", "issues": []}

    monkeypatch.setattr(runner, "refiner_run", fake_refiner_run)
    monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: FakeExecutor())
    return calls


def _reply(sql: str) -> dict:
    return {"choices": [{"message": {"content": f"<solution>\n{sql}\n</solution>"}}]}


def _run(out_dir: Path) -> tuple:
    inst = runner.Instance(instance_id="local999", db="testdb", question="q")
    return runner.perform_refinement_and_revision(
        inst=inst,
        final_sql=ORIGINAL_SQL,
        predicted_cte_hint=None,
        engine="sqlite",
        db_path_or_cred=None,
        messages=[],
        out_dir=out_dir,
        model="gpt-4.1",
        verbose=False,
    )


def test_later_cte_is_read_from_the_adopted_solution(tmp_path, monkeypatch, refiner_calls):
    monkeypatch.setattr(runner, "llm_completion", lambda **_k: _reply(REVISED_SQL))

    _run(tmp_path)

    cte_calls = _cte_calls(refiner_calls)
    assert len(cte_calls) == 2
    assert "SELECT 20 AS y" in cte_calls[1]["cte_text"]
    assert "SELECT 2 AS y" not in cte_calls[1]["cte_text"]


def test_previous_ctes_are_read_from_the_adopted_solution(tmp_path, monkeypatch, refiner_calls):
    monkeypatch.setattr(runner, "llm_completion", lambda **_k: _reply(REVISED_SQL))

    _run(tmp_path)

    previous = _cte_calls(refiner_calls)[1]["previous_ctes"]
    assert "SELECT 10 AS x" in previous
    assert "SELECT 1 AS x" not in previous


def test_final_select_sees_the_adopted_ctes(tmp_path, monkeypatch, refiner_calls):
    monkeypatch.setattr(runner, "llm_completion", lambda **_k: _reply(REVISED_SQL))

    final_sql, _ = _run(tmp_path)

    final_calls = [c for c in refiner_calls if c["cte_goal"].startswith("Final SELECT")]
    assert len(final_calls) == 1
    assert "SELECT 20 AS y" in final_calls[0]["cte_text"]
    assert final_sql == REVISED_SQL


def test_loop_ends_when_the_adopted_solution_drops_a_cte(tmp_path, monkeypatch, refiner_calls):
    """A shorter revision must not be indexed past its end."""
    monkeypatch.setattr(runner, "llm_completion", lambda **_k: _reply(SINGLE_CTE_SQL))

    _run(tmp_path)

    assert len(_cte_calls(refiner_calls)) == 1


def test_every_cte_is_visited_when_nothing_needs_revision(tmp_path, monkeypatch):
    """The happy path is unchanged: no revision, every CTE still refined once."""
    calls = []

    def fake_refiner_run(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "issues": []}

    monkeypatch.setattr(runner, "refiner_run", fake_refiner_run)
    monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: FakeExecutor())
    monkeypatch.setattr(
        runner,
        "llm_completion",
        lambda **_k: pytest.fail("no revision expected when the refiner reports ok"),
    )

    final_sql, _ = _run(tmp_path)

    assert [c["cte_text"] for c in _cte_calls(calls)] == [
        "WITH customer_totals AS (\nSELECT 1 AS x FROM t1\n)",
        "WITH monthly_revenue AS (\nSELECT 2 AS y FROM t2\n)",
    ]
    assert final_sql == ORIGINAL_SQL
