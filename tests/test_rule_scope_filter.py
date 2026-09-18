"""Restricting which rule scopes reach the refiner.

The reference run injected a median of 12 rules per fragment, 95% of them
`scope='generic'`, and the harm concentrated on the instances that could only ever
receive generic rules (net -11.8% per instance vs -3.8% where db-scoped rules were
also available). Dropping generic rules is the direct test of that.

Filtering happens here rather than in `tkstore/tagger_index.py` so the vendored
upstream retrieval stays untouched, and it happens *before* FilterKnowledge so the
LLM never spends a call ranking rules that are already excluded.
"""

import json

import pytest

from src.agents import sql_agent_runner as runner


CANDIDATES = [
    {"mem_id": "1", "scope": "generic", "rule": "cast integer division to real"},
    {"mem_id": "2", "scope": "db", "rule": "orders.status is capitalised"},
    {"mem_id": "3", "scope": "GENERIC", "rule": "prefer strftime over julianday"},
    {"mem_id": "4", "scope": "db", "rule": "player.debut can be empty string"},
]


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


@pytest.fixture
def store_csv(tmp_path) -> str:
    path = tmp_path / "tkstore_sqlite.csv"
    path.write_text("mem_id,rule\n1,x\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def stub_search(monkeypatch):
    monkeypatch.setattr(runner, "search_index_for_sql", lambda *_a, **_k: list(CANDIDATES))


class TestParserDefaults:
    def test_all_scopes_by_default(self):
        """The reference run used every scope; changing the default would silently
        reinterpret it."""
        assert _args(["--instance-id", "local001"]).rule_scope == "all"

    def test_db_and_generic_can_be_selected(self):
        assert _args(["--instance-id", "local001", "--rule-scope", "db"]).rule_scope == "db"
        assert _args(["--instance-id", "local001", "--rule-scope", "generic"]).rule_scope == "generic"

    def test_an_unknown_scope_is_rejected(self):
        with pytest.raises(SystemExit):
            _args(["--instance-id", "local001", "--rule-scope", "nonsense"])


class TestKnowledgeOptionsCarryTheScope:
    def test_the_scope_travels_with_the_store(self, store_csv):
        options = runner._knowledge_options(
            _args(["--instance-id", "local001", "--refine-cte",
                   "--tkstore", store_csv, "--rule-scope", "db"])
        )

        assert options["rule_scope"] == "db"

    def test_a_scope_without_a_store_is_rejected(self):
        """It would read as an ablation that ran, when no rule was retrieved at all."""
        with pytest.raises(ValueError, match="rule-scope"):
            runner._knowledge_options(
                _args(["--instance-id", "local001", "--refine-cte", "--rule-scope", "db"])
            )


class TestRetrievalHonoursTheScope:
    def _retrieve(self, scope):
        return runner._retrieve_rules_for(
            sql_text="SELECT 1",
            tkstore_path="store.csv",
            db="testdb",
            use_llm_filtering=False,
            filter_model="gpt-4.1",
            rule_scope=scope,
        )

    def test_all_keeps_every_candidate(self, stub_search):
        candidates, selected = self._retrieve("all")

        assert [c["mem_id"] for c in candidates] == ["1", "2", "3", "4"]
        assert [s["mem_id"] for s in selected] == ["1", "2", "3", "4"]

    def test_db_drops_the_generic_rules(self, stub_search):
        candidates, selected = self._retrieve("db")

        assert [c["mem_id"] for c in candidates] == ["2", "4"]
        assert [s["mem_id"] for s in selected] == ["2", "4"]

    def test_generic_drops_the_db_rules(self, stub_search):
        candidates, _ = self._retrieve("generic")

        assert [c["mem_id"] for c in candidates] == ["1", "3"]

    def test_the_scope_comparison_ignores_case(self, stub_search):
        """The store spells it both `generic` and `GENERIC`."""
        candidates, _ = self._retrieve("generic")

        assert "3" in [c["mem_id"] for c in candidates]

    def test_filterknowledge_never_sees_an_excluded_rule(self, monkeypatch, stub_search):
        seen = {}

        def fake_filter(sql_text, candidates, db=None, model=None):
            seen["ids"] = [c["mem_id"] for c in candidates]
            return candidates

        monkeypatch.setattr(runner, "_llm_filter_relevant_rules", fake_filter)
        monkeypatch.setattr(runner, "_is_openai_provider", lambda: False)

        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path="store.csv", db="testdb",
            use_llm_filtering=True, filter_model="gpt-4.1", rule_scope="db",
        )

        assert seen["ids"] == ["2", "4"]

    def test_a_fragment_can_end_up_with_no_rules(self, monkeypatch):
        """34 of the 86 instances have no db-scoped rule at all, so under `db` their
        arm degenerates into the knowledge-free one. That has to be a normal outcome,
        not an error."""
        monkeypatch.setattr(
            runner, "search_index_for_sql",
            lambda *_a, **_k: [c for c in CANDIDATES if c["scope"].lower() == "generic"],
        )

        candidates, selected = self._retrieve("db")

        assert candidates == []
        assert selected == []


class TestTheReportRecordsTheScope:
    """Two arms differing only by scope produce otherwise identical reports, so the
    artifact has to say which one it was."""

    def test_the_scope_is_written_to_retrieved_rules(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "refiner_run", lambda **_k: {"status": "ok", "issues": []})
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: _FakeExecutor())
        monkeypatch.setattr(runner, "_retrieve_rules_for", lambda **_k: ([], []))

        runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb", question="q"),
            final_sql="SELECT 1 FROM t",
            predicted_cte_hint=None,
            engine="sqlite",
            db_path_or_cred=str(tmp_path / "x.sqlite"),
            messages=[],
            out_dir=tmp_path,
            model="gpt-4.1",
            verbose=False,
            tkstore_path=str(tmp_path / "store.csv"),
            rule_scope="db",
        )

        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        assert report["rule_scope"] == "db"


class _FakeExecutor:
    def execute(self, sql):
        return ["x"], [(1,)]

    def close(self):
        pass
