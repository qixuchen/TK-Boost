"""Algorithm 5 injects retrieved knowledge into the refiner, per CTE.

`run_refiner` does no retrieval of its own, so the only channel is the `cte_goal`
string. These tests pin down what reaches that string and what gets recorded for
after-the-fact attribution.
"""

import json
from pathlib import Path

import pytest

from src.agents import sql_agent_runner as runner


TWO_CTE_SQL = """WITH customer_totals AS (
SELECT 1 AS x FROM t1
),
monthly_revenue AS (
SELECT 2 AS y FROM t2
)
SELECT * FROM monthly_revenue"""

NO_CTE_SQL = "SELECT order_id FROM orders WHERE status = 'delivered'"

RULES = [
    {"mem_id": "26", "scope": "generic", "rule": "Cast integer division to REAL first."},
    {"mem_id": "31", "scope": "db", "rule": "orders.status is stored capitalised."},
]

KNOWLEDGE_HEADER = "Use these tribal knowledge rules as guidance:"


class FakeExecutor:
    def execute(self, sql):
        return ["x"], [(1,)]

    def close(self):
        pass


def _cte_calls(calls: list) -> list:
    return [c for c in calls if not c["cte_goal"].startswith("Final SELECT")]


def _final_calls(calls: list) -> list:
    return [c for c in calls if c["cte_goal"].startswith("Final SELECT")]


@pytest.fixture
def refiner_calls(monkeypatch):
    """Record refiner invocations; nothing needs revising, so no agent loop runs."""
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
    return calls


@pytest.fixture
def stub_retrieval(monkeypatch):
    """Replace Algorithm 4 with a recorder returning a fixed rule set."""
    calls = []

    def fake_retrieve(**kwargs):
        calls.append(kwargs)
        return RULES, RULES[:1]

    monkeypatch.setattr(runner, "_retrieve_rules_for", fake_retrieve)
    return calls


def _run(out_dir: Path, final_sql: str = TWO_CTE_SQL, **kwargs) -> tuple:
    inst = runner.Instance(instance_id="local999", db="testdb", question="q")
    return runner.perform_refinement_and_revision(
        inst=inst,
        final_sql=final_sql,
        predicted_cte_hint=None,
        engine="sqlite",
        db_path_or_cred=None,
        messages=[],
        out_dir=out_dir,
        model="gpt-4.1",
        verbose=False,
        **kwargs,
    )


class TestInjectionIntoTheRefinerGoal:
    def test_cte_goal_carries_the_selected_rules(self, tmp_path, refiner_calls, stub_retrieval):
        _run(tmp_path, tkstore_path="store.csv")

        goal = _cte_calls(refiner_calls)[0]["cte_goal"]
        assert goal.startswith("CTE customer_totals validation")
        assert KNOWLEDGE_HEADER in goal
        assert "- Cast integer division to REAL first." in goal

    def test_only_selected_rules_reach_the_prompt(self, tmp_path, refiner_calls, stub_retrieval):
        """The stub selects one of two candidates; the dropped one must not leak."""
        _run(tmp_path, tkstore_path="store.csv")

        goal = _cte_calls(refiner_calls)[0]["cte_goal"]
        assert "orders.status is stored capitalised." not in goal

    def test_final_select_goal_carries_the_selected_rules(self, tmp_path, refiner_calls, stub_retrieval):
        _run(tmp_path, tkstore_path="store.csv")

        goal = _final_calls(refiner_calls)[0]["cte_goal"]
        assert goal.startswith("Final SELECT using 2 CTE(s)")
        assert "- Cast integer division to REAL first." in goal

    def test_sql_without_ctes_still_receives_rules(self, tmp_path, refiner_calls, stub_retrieval):
        """No WITH clause means the whole query goes through the final-SELECT pass."""
        _run(tmp_path, final_sql=NO_CTE_SQL, tkstore_path="store.csv")

        assert _cte_calls(refiner_calls) == []
        goal = _final_calls(refiner_calls)[0]["cte_goal"]
        assert "- Cast integer division to REAL first." in goal

    def test_goal_is_untouched_without_a_store(self, tmp_path, refiner_calls):
        _run(tmp_path)

        assert _cte_calls(refiner_calls)[0]["cte_goal"] == "CTE customer_totals validation"
        assert _final_calls(refiner_calls)[0]["cte_goal"] == "Final SELECT using 2 CTE(s)"

    def test_goal_is_untouched_when_retrieval_finds_nothing(self, tmp_path, monkeypatch, refiner_calls):
        """An empty knowledge block would waste context and mislead the refiner."""
        monkeypatch.setattr(runner, "_retrieve_rules_for", lambda **_k: ([], []))

        _run(tmp_path, tkstore_path="store.csv")

        goal = _cte_calls(refiner_calls)[0]["cte_goal"]
        assert goal == "CTE customer_totals validation"
        assert KNOWLEDGE_HEADER not in goal


class TestRetrievedRulesReport:
    def test_report_records_candidates_and_selected_per_stage(self, tmp_path, refiner_calls, stub_retrieval):
        _run(tmp_path, tkstore_path="store.csv")

        report = json.loads((tmp_path / "retrieved_rules.json").read_text(encoding="utf-8"))
        stages = [(r["stage"], r["name"]) for r in report["retrievals"]]
        assert stages == [
            ("cte", "customer_totals"),
            ("cte", "monthly_revenue"),
            ("final_select", "_final_select"),
        ]
        assert report["retrievals"][0]["candidates"] == ["26", "31"]
        assert report["retrievals"][0]["selected"] == ["26"]

    def test_report_records_the_cte_count(self, tmp_path, refiner_calls, stub_retrieval):
        """C17 can still parse a query into zero CTEs; that has to be visible."""
        _run(tmp_path, final_sql=NO_CTE_SQL, tkstore_path="store.csv")

        report = json.loads((tmp_path / "retrieved_rules.json").read_text(encoding="utf-8"))
        assert report["n_ctes"] == 0
        assert report["filter_model"] == "gpt-4.1"
        assert report["use_llm_filtering"] is True

    def test_no_report_is_written_without_a_store(self, tmp_path, refiner_calls):
        _run(tmp_path)

        assert not (tmp_path / "retrieved_rules.json").exists()


class TestRetrievalCall:
    """`_retrieve_rules_for` is our Algorithm 4 entry point."""

    @pytest.fixture
    def search_calls(self, monkeypatch):
        calls = []

        def fake_search(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return RULES

        monkeypatch.setattr(runner, "search_index_for_sql", fake_search)
        return calls

    @pytest.fixture
    def filter_calls(self, monkeypatch):
        calls = []

        def fake_filter(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return RULES[:1]

        monkeypatch.setattr(runner, "_llm_filter_relevant_rules", fake_filter)
        return calls

    def test_database_specific_rules_are_kept(self, search_calls, filter_calls):
        """generic_only=True would drop every scope='db' rule (B7)."""
        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
            use_llm_filtering=False, filter_model="gpt-4.1",
        )

        assert search_calls[0]["kwargs"]["generic_only"] is False
        assert search_calls[0]["kwargs"]["db"] == "testdb"

    def test_instance_id_is_never_forwarded(self, search_calls, filter_calls):
        """The instance_id filter is a no-op that silently returns everything (C14)."""
        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
            use_llm_filtering=False, filter_model="gpt-4.1",
        )

        assert "instance_id" not in search_calls[0]["kwargs"]

    def test_llm_filtering_narrows_the_candidates(self, search_calls, filter_calls):
        candidates, selected = runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
            use_llm_filtering=True, filter_model="gpt-4.1",
        )

        assert candidates == RULES
        assert selected == RULES[:1]
        assert filter_calls[0]["kwargs"]["model"] == "gpt-4.1"

    def test_filtering_is_skipped_when_disabled(self, search_calls, filter_calls):
        candidates, selected = runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
            use_llm_filtering=False, filter_model="gpt-4.1",
        )

        assert filter_calls == []
        assert selected == candidates

    def test_azure_model_names_are_mapped_for_openai(self, monkeypatch, search_calls, filter_calls):
        """tkstore.tagger_index has no provider mapping of its own, so an unmapped
        'azure/...' name raises inside the filter and is swallowed into 'no filtering'."""
        monkeypatch.setattr(runner, "_is_openai_provider", lambda: True)

        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
            use_llm_filtering=True, filter_model="azure/gpt-4.1",
        )

        assert filter_calls[0]["kwargs"]["model"] == "gpt-4.1"

    def test_unmappable_azure_model_fails_loudly(self, monkeypatch, search_calls, filter_calls):
        """The filter swallows its own errors and returns every candidate, so an
        unusable model has to be rejected before it silently disables filtering."""
        monkeypatch.setattr(runner, "_is_openai_provider", lambda: True)

        with pytest.raises(ValueError, match="azure/gpt-9"):
            runner._retrieve_rules_for(
                sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
                use_llm_filtering=True, filter_model="azure/gpt-9",
            )

        assert filter_calls == []

    def test_azure_model_is_allowed_on_azure_credentials(self, monkeypatch, search_calls, filter_calls):
        monkeypatch.setattr(runner, "_is_openai_provider", lambda: False)

        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
            use_llm_filtering=True, filter_model="azure/gpt-9",
        )

        assert filter_calls[0]["kwargs"]["model"] == "azure/gpt-9"
