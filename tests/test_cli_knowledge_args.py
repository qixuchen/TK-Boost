"""CLI plumbing for the tribal-knowledge flags.

Retrieval only happens inside `perform_refinement_and_revision`, which in turn only
runs under --refine-cte or --refine-output. A --tkstore that reaches neither would
quietly produce a baseline run, so the combination is rejected up front.
"""

import json
from pathlib import Path

import pytest

from src.agents import sql_agent_runner as runner


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


@pytest.fixture
def store_csv(tmp_path) -> str:
    path = tmp_path / "tkstore_sqlite.csv"
    path.write_text("mem_id,rule\n1,do not divide integers\n", encoding="utf-8")
    return str(path)


class TestParserDefaults:
    def test_knowledge_is_off_by_default(self):
        args = _args(["--instance-id", "local001"])

        assert args.tkstore is None
        assert args.no_llm_filtering is False
        assert args.filter_model == "gpt-4.1"

    def test_flags_are_accepted(self, store_csv):
        args = _args([
            "--instance-id", "local001", "--refine-cte",
            "--tkstore", store_csv, "--no-llm-filtering",
            "--filter-model", "o4-mini",
        ])

        assert args.tkstore == store_csv
        assert args.no_llm_filtering is True
        assert args.filter_model == "o4-mini"

    def test_existing_scope_flag_is_untouched(self):
        """--tribalknowledge-all-scopes is a dead parameter (B12) and stays that way."""
        args = _args(["--instance-id", "local001", "--tribalknowledge-all-scopes"])

        assert args.tribalknowledge_all_scopes is True


class TestKnowledgeOptions:
    def test_no_store_means_no_knowledge_kwargs(self):
        options = runner._knowledge_options(_args(["--instance-id", "local001", "--refine-cte"]))

        assert options == {}

    def test_store_with_refine_cte(self, store_csv):
        args = _args(["--instance-id", "local001", "--refine-cte", "--tkstore", store_csv])

        assert runner._knowledge_options(args) == {
            "tkstore_path": store_csv,
            "use_llm_filtering": True,
            "filter_model": "gpt-4.1",
            "cross_db_generic": "never",
            "context_filter": False,
        }

    def test_filtering_can_be_disabled(self, store_csv):
        args = _args([
            "--instance-id", "local001", "--refine-cte",
            "--tkstore", store_csv, "--no-llm-filtering",
        ])

        assert runner._knowledge_options(args)["use_llm_filtering"] is False

    def test_store_is_allowed_in_refinement_only_mode(self, store_csv, tmp_path):
        """--refine-output always refines, so it does not need --refine-cte."""
        args = _args(["--refine-output", str(tmp_path), "--tkstore", store_csv])

        assert runner._knowledge_options(args)["tkstore_path"] == store_csv

    def test_store_without_a_refinement_stage_is_rejected(self, store_csv):
        args = _args(["--instance-id", "local001", "--tkstore", store_csv])

        with pytest.raises(ValueError, match="--refine-cte"):
            runner._knowledge_options(args)

    def test_missing_store_file_is_rejected(self, tmp_path):
        args = _args([
            "--instance-id", "local001", "--refine-cte",
            "--tkstore", str(tmp_path / "absent.csv"),
        ])

        with pytest.raises(ValueError, match="absent.csv"):
            runner._knowledge_options(args)


class TestRefinementOnlyPassthrough:
    """--refine-output is the cheap re-run path; it must not drop the knowledge."""

    @pytest.fixture
    def refinement_tree(self, tmp_path, monkeypatch):
        source = tmp_path / "outputs_src"
        (source / "local999_20260101_000000").mkdir(parents=True)
        (source / "local999_20260101_000000" / "execution_query.sql").write_text(
            "WITH customer_totals AS (\nSELECT 1\n)\nSELECT * FROM customer_totals",
            encoding="utf-8",
        )

        inst = runner.Instance(instance_id="local999", db="testdb", question="q")
        monkeypatch.setattr(runner, "load_instances_from_jsonl", lambda _p: [inst])
        monkeypatch.setattr(runner, "resolve_sqlite_db_path", lambda *_a: "/tmp/fake.sqlite")
        monkeypatch.setattr(runner, "get_system_prompt", lambda *_a, **_k: "system")
        monkeypatch.setattr(runner, "build_user_message", lambda *_a, **_k: "user")
        monkeypatch.setattr(runner, "_choose_and_mark_final_artifacts", lambda *_a, **_k: None)
        return source

    def test_knowledge_reaches_the_refinement_call(self, refinement_tree, tmp_path, store_csv, monkeypatch):
        calls = []
        monkeypatch.setattr(
            runner,
            "perform_refinement_and_revision",
            lambda **kwargs: (calls.append(kwargs), ("SELECT 1", None))[1],
        )

        runner.run_refinement_on_existing_outputs(_args([
            "--refine-output", str(refinement_tree),
            "--refine-output-dir", str(tmp_path / "dest"),
            "--tkstore", store_csv,
        ]))

        assert len(calls) == 1
        assert calls[0]["tkstore_path"] == store_csv
        assert calls[0]["use_llm_filtering"] is True
        assert calls[0]["filter_model"] == "gpt-4.1"

    def test_no_knowledge_kwargs_without_a_store(self, refinement_tree, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            runner,
            "perform_refinement_and_revision",
            lambda **kwargs: (calls.append(kwargs), ("SELECT 1", None))[1],
        )

        runner.run_refinement_on_existing_outputs(_args([
            "--refine-output", str(refinement_tree),
            "--refine-output-dir", str(tmp_path / "dest"),
        ]))

        assert len(calls) == 1
        assert "tkstore_path" not in calls[0]
