"""Stopping generic rules from crossing into databases they were never mined from.

Stage 8. Round H localised the harm precisely: grouped by whether the instance's database
has any db-scoped rule at all, the 34 instances with none went -2 while the 52 with some
went +1. Those 34 can only ever receive generic rules mined from *other* databases, and
three of the four regressions read no rule from their own database -- local061 rewrote an
INNER JOIN into a key UNION on a rule from California_Traffic_Collision, local330 added
SELECT DISTINCT to an event log on the same one.

Retrieval allows this by design (`tagger_index.py:566-572`): a row is kept when its scope
is 'generic' OR its db matches, and the `is_generic` branch short-circuits the db check
entirely. Every one of the store's 52 generic rules carries a concrete database name and
none carries 'all', so the information needed to stop it is already recorded -- it is just
never consulted.

Note this is the *only* field that can exclude a generic rule: all 52 carry table='all'
and column='all', 49 carry nulls='all', and data_type and sql_operations already filter.
So "filter generic rules on every field" reduces to filtering on db.

Two cuts, because they differ on the 52 instances that were not harmed:
  never     generic rules never cross a database boundary
  known-db  they may, but only into a database the store already has db-scoped rules for
"""

import csv
import json

import pytest

from src.agents import sql_agent_runner as runner


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


STORE_ROWS = [
    # mem_id, db, scope
    ("1", "California_Traffic_Collision", "generic"),
    ("2", "SQLITE_SAKILA", "generic"),
    ("3", "bank_sales_trading", "generic"),
    ("4", "bank_sales_trading", "db"),
    ("5", "log", "generic"),
    ("6", "AdventureWorks", "generic"),
    ("7", "all", "generic"),
]


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "store.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mem_id", "instance_id", "db", "scope", "sql_operations",
                    "table", "column", "data_type", "nulls", "rule"])
        for mem_id, db, scope in STORE_ROWS:
            w.writerow([mem_id, f"local{mem_id}", db, scope, "select",
                        "all", "all", "all", "all", f"rule {mem_id}"])
    return str(path)


def _cands(*mem_ids_and_scopes):
    """Candidates shaped like `search_index_for_sql` returns them -- note no `db` field."""
    return [{"mem_id": m, "scope": s, "rule": f"rule {m}"} for m, s in mem_ids_and_scopes]


class TestTheStoreIndex:
    """`search_index_for_sql` reads each row's db but does not return it, so the mapping
    has to be recovered from the store to filter on it."""

    def test_it_maps_each_rule_to_its_database(self, store):
        index = runner._store_rule_index(store)

        assert index.db_of["1"] == "California_Traffic_Collision"
        assert index.db_of["5"] == "log"

    def test_it_knows_which_databases_have_db_scoped_rules(self, store):
        index = runner._store_rule_index(store)

        assert index.has_db_rules("bank_sales_trading") is True
        assert index.has_db_rules("log") is False
        assert index.has_db_rules("California_Traffic_Collision") is False

    def test_database_lookup_ignores_case(self, store):
        """The real store carries both 'AdventureWorks' and 'adventureworks'."""
        index = runner._store_rule_index(store)

        assert index.db_of["6"] == "AdventureWorks"
        assert index.has_db_rules("BANK_SALES_TRADING") is True

    def test_an_unreadable_store_yields_an_empty_index(self, tmp_path):
        """Matches `_store_provenance`: a broken store must not lose the whole run."""
        index = runner._store_rule_index(str(tmp_path / "missing.csv"))

        assert index.db_of == {}
        assert index.has_db_rules("anything") is False


class TestTheFilterLeavesEverythingAloneByDefault:
    def test_always_returns_the_candidates_unchanged(self, store):
        candidates = _cands(("1", "generic"), ("4", "db"))

        kept = runner._filter_cross_db_generic(
            candidates, tkstore_path=store, db="log", mode="always")

        assert kept == candidates

    def test_the_store_is_not_even_read(self, tmp_path):
        """The default path must not depend on the store being parseable."""
        candidates = _cands(("1", "generic"))

        kept = runner._filter_cross_db_generic(
            candidates, tkstore_path=str(tmp_path / "missing.csv"), db="log", mode="always")

        assert kept == candidates


class TestNeverCrossing:
    def test_a_generic_rule_from_another_database_is_dropped(self, store):
        kept = runner._filter_cross_db_generic(
            _cands(("1", "generic")), tkstore_path=store, db="log", mode="never")

        assert kept == []

    def test_a_generic_rule_from_this_database_is_kept(self, store):
        kept = runner._filter_cross_db_generic(
            _cands(("5", "generic")), tkstore_path=store, db="log", mode="never")

        assert [r["mem_id"] for r in kept] == ["5"]

    def test_db_scoped_rules_are_untouched(self, store):
        """Retrieval already required their db to match, so re-filtering them would only
        risk dropping a rule for a reason this mode is not about."""
        kept = runner._filter_cross_db_generic(
            _cands(("4", "db")), tkstore_path=store, db="bank_sales_trading", mode="never")

        assert [r["mem_id"] for r in kept] == ["4"]

    def test_a_rule_marked_for_all_databases_is_kept(self, store):
        """None exist in the upstream store, but the field is honoured upstream."""
        kept = runner._filter_cross_db_generic(
            _cands(("7", "generic")), tkstore_path=store, db="log", mode="never")

        assert [r["mem_id"] for r in kept] == ["7"]

    def test_the_comparison_ignores_case(self, store):
        kept = runner._filter_cross_db_generic(
            _cands(("6", "generic")), tkstore_path=store, db="adventureworks", mode="never")

        assert [r["mem_id"] for r in kept] == ["6"]

    def test_a_rule_missing_from_the_store_is_kept(self, store):
        """An id the store does not explain means the two disagree; dropping it silently
        would hide that, so it is kept and shows up in the report instead."""
        kept = runner._filter_cross_db_generic(
            _cands(("999", "generic")), tkstore_path=store, db="log", mode="never")

        assert [r["mem_id"] for r in kept] == ["999"]

    def test_order_is_preserved(self, store):
        kept = runner._filter_cross_db_generic(
            _cands(("5", "generic"), ("1", "generic"), ("7", "generic")),
            tkstore_path=store, db="log", mode="never")

        assert [r["mem_id"] for r in kept] == ["5", "7"]


class TestCrossingOnlyIntoAKnownDatabase:
    """The narrower cut: leave the 52 instances that were not harmed exactly as round H had
    them, and close generic injection only where there is no db-scoped rule to anchor it."""

    def test_foreign_generic_is_kept_when_the_database_has_db_rules(self, store):
        kept = runner._filter_cross_db_generic(
            _cands(("1", "generic"), ("4", "db")),
            tkstore_path=store, db="bank_sales_trading", mode="known-db")

        assert [r["mem_id"] for r in kept] == ["1", "4"]

    def test_foreign_generic_is_dropped_when_it_does_not(self, store):
        kept = runner._filter_cross_db_generic(
            _cands(("1", "generic"), ("5", "generic")),
            tkstore_path=store, db="log", mode="known-db")

        assert [r["mem_id"] for r in kept] == ["5"], "its own generic rule stays"

    def test_a_database_absent_from_the_store_keeps_nothing_generic(self, store):
        """The 34 harmed instances: their database has neither db nor generic rules."""
        kept = runner._filter_cross_db_generic(
            _cands(("1", "generic"), ("2", "generic")),
            tkstore_path=store, db="complex_oracle", mode="known-db")

        assert kept == []


class TestRetrievalWiring:
    @pytest.fixture
    def seen(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(runner, "search_index_for_sql",
                            lambda *a, **k: _cands(("1", "generic"), ("5", "generic")))

        def fake_filter(sql_text, candidates, db=None, model=None):
            calls["given"] = [r["mem_id"] for r in candidates]
            return candidates
        monkeypatch.setattr(runner, "_llm_filter_relevant_rules", fake_filter)
        return calls

    def test_the_drop_happens_before_filterknowledge(self, store, seen):
        """Otherwise the model is asked about rules that are about to be discarded, and the
        report's candidate list would not match what was considered."""
        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="log",
            use_llm_filtering=True, filter_model="gpt-4.1", cross_db_generic="never")

        assert seen["given"] == ["5"]

    def test_both_returned_lists_reflect_the_drop(self, store, seen):
        candidates, selected = runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="log",
            use_llm_filtering=True, filter_model="gpt-4.1", cross_db_generic="never")

        assert [r["mem_id"] for r in candidates] == ["5"]
        assert [r["mem_id"] for r in selected] == ["5"]

    def test_the_default_changes_nothing(self, store, seen):
        candidates, _ = runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="log",
            use_llm_filtering=True, filter_model="gpt-4.1")

        assert [r["mem_id"] for r in candidates] == ["1", "5"]

    def test_it_applies_without_llm_filtering_too(self, store, monkeypatch):
        monkeypatch.setattr(runner, "search_index_for_sql",
                            lambda *a, **k: _cands(("1", "generic"), ("5", "generic")))

        candidates, selected = runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="log",
            use_llm_filtering=False, filter_model="gpt-4.1", cross_db_generic="never")

        assert [r["mem_id"] for r in candidates] == ["5"]
        assert selected == candidates


class TestCli:
    def test_it_defaults_to_never(self):
        assert _args(["--instance-id", "local001"]).cross_db_generic == "never"

    def test_a_store_without_the_flag_gets_never(self, store):
        options = runner._knowledge_options(_args([
            "--instance-id", "local001", "--refine-cte", "--tkstore", store,
        ]))

        assert options["cross_db_generic"] == "never"

    def test_the_modes_are_carried_in_the_knowledge_options(self, store):
        options = runner._knowledge_options(_args([
            "--instance-id", "local001", "--refine-cte", "--tkstore", store,
            "--cross-db-generic", "always",
        ]))

        assert options["cross_db_generic"] == "always"

    def test_an_unknown_mode_is_refused_by_the_parser(self):
        with pytest.raises(SystemExit):
            _args(["--instance-id", "local001", "--cross-db-generic", "sometimes"])

    def test_without_a_store_the_default_does_not_block_the_run(self):
        """never is now the default, so requiring a store whenever the mode is not
        always would refuse every no-knowledge run."""
        assert runner._knowledge_options(
            _args(["--instance-id", "local001", "--refine-cte"])) == {}


TWO_CTE_SQL = """WITH totals AS (
SELECT 1 AS n FROM t
),
shares AS (
SELECT n FROM totals
)
SELECT * FROM shares"""


class _Executor:
    def execute(self, sql):
        return ["n"], [(1,)]

    def close(self):
        pass


class TestTheReportRecordsIt:
    def test_the_mode_appears_in_retrieved_rules(self, tmp_path, store, monkeypatch):
        monkeypatch.setattr(runner, "refiner_run",
                            lambda **kw: {"status": "ok", "issues": []})
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: _Executor())
        monkeypatch.setattr(runner, "write_csv", lambda *_a, **_k: None)
        monkeypatch.setattr(runner, "_retrieve_rules_for", lambda **_k: ([], []))

        runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb", question="q"),
            final_sql=TWO_CTE_SQL, predicted_cte_hint=None, engine="sqlite",
            db_path_or_cred=str(tmp_path / "x.sqlite"), messages=[], out_dir=tmp_path,
            model="gpt-4.1", verbose=False, tkstore_path=store,
            cross_db_generic="known-db",
        )

        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        assert report["cross_db_generic"] == "known-db"

    def test_the_mode_reaches_retrieval(self, tmp_path, store, monkeypatch):
        seen = {}
        monkeypatch.setattr(runner, "refiner_run",
                            lambda **kw: {"status": "ok", "issues": []})
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: _Executor())
        monkeypatch.setattr(runner, "write_csv", lambda *_a, **_k: None)
        monkeypatch.setattr(runner, "_retrieve_rules_for",
                            lambda **kw: seen.setdefault("mode", kw.get("cross_db_generic")) and ([], []) or ([], []))

        runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb", question="q"),
            final_sql=TWO_CTE_SQL, predicted_cte_hint=None, engine="sqlite",
            db_path_or_cred=str(tmp_path / "x.sqlite"), messages=[], out_dir=tmp_path,
            model="gpt-4.1", verbose=False, tkstore_path=store,
            cross_db_generic="never",
        )

        assert seen["mode"] == "never"
