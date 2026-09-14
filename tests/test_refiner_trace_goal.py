"""The refiner's trace has to show the goal it was actually given.

`cte_goal` is the only channel through which tribal knowledge reaches the refiner,
so without it in the trace there is no way to audit from the artifacts which rules
a verdict was based on.
"""

import sqlite3

import pytest

from src.agents import cte_refiner


GOAL_WITH_RULES = (
    "CTE monthly_totals validation\n\n"
    "Use these tribal knowledge rules as guidance:\n\n"
    "- Cast integer division to REAL before dividing.\n"
    "- customer_transactions.txn_type is lowercase."
)


@pytest.fixture
def sqlite_db(tmp_path) -> str:
    path = tmp_path / "probe.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def stub_llm(monkeypatch):
    """One canned verdict; max_turns=1 keeps the probe loop from running."""
    def fake_llm(model, messages, **kwargs):
        return {"choices": [{"message": {
            "content": "<verdict_json>{\"status\": \"ok\", \"issues\": []}</verdict_json>",
        }}]}

    monkeypatch.setattr(cte_refiner, "llm", fake_llm)


def _run(trace_path, db_path, goal=GOAL_WITH_RULES):
    return cte_refiner.run_refiner(
        instance_id="local999",
        db_id="testdb",
        user_query="how many customers",
        cte_text="WITH monthly_totals AS (\nSELECT 1 AS id\n)",
        cte_goal=goal,
        model="gpt-4.1",
        max_turns=1,
        verbose=False,
        trace_output_path=str(trace_path),
        db_path=db_path,
    )


def test_trace_records_the_goal(tmp_path, sqlite_db, stub_llm):
    trace_path = tmp_path / "trace.txt"

    _run(trace_path, sqlite_db)

    trace = trace_path.read_text(encoding="utf-8")
    assert "CTE GOAL" in trace
    assert "- Cast integer division to REAL before dividing." in trace
    assert "- customer_transactions.txn_type is lowercase." in trace


def test_trace_still_records_the_user_query(tmp_path, sqlite_db, stub_llm):
    """Existing sections must survive."""
    trace_path = tmp_path / "trace.txt"

    _run(trace_path, sqlite_db)

    assert "how many customers" in trace_path.read_text(encoding="utf-8")


def test_goal_without_rules_is_recorded_verbatim(tmp_path, sqlite_db, stub_llm):
    trace_path = tmp_path / "trace.txt"

    _run(trace_path, sqlite_db, goal="Final SELECT using 0 CTE(s)")

    trace = trace_path.read_text(encoding="utf-8")
    assert "Final SELECT using 0 CTE(s)" in trace
    assert "tribal knowledge rules" not in trace
