"""Handing the refiner's candidate SQL to the agent instead of discarding it.

Stage 5 of `implementation_plan.md`. The refiner emits a complete corrected fragment in
`suggested_fix_sql`, but the default feedback only forwards the prose `suggested_fix`, so
the agent that actually rewrites has never seen a rule -- measured similarity between its
rewrite and the refiner's suggestion is 0.23-0.47.

Adopting the refiner's SQL outright (stage E) fixed the transfer but broke output
contracts in 33 of 108 cases, because the refiner sees one CTE and cannot know what
downstream consumers read. Putting the candidate SQL *in the feedback* keeps the agent as
the integrator -- Alg 5 line 7 still has the agent author `s_t` -- while letting the
knowledge-informed SQL through.
"""

import json

import pytest

from src.agents import sql_agent_runner as runner


INSTRUCTION = ("\nInstruction: Revise ONLY the CTE named 'totals' in your previous solution.\n"
               "Output a complete <solution>.")

CANDIDATE = "WITH totals AS (\nSELECT 1.0 * n / SUM(n) OVER () AS share FROM t\n)"


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


def _verdict(**over):
    base = {"status": "issues", "issues": ["wrong grain", "missing share"],
            "suggested_fix": "compute a share, not a raw count",
            "suggested_fix_sql": CANDIDATE,
            "tests": ["SELECT COUNT(*) FROM t"]}
    base.update(over)
    return base


class TestTheDefaultRenderingIsFrozen:
    """Rounds A-E were all measured with the prose-only feedback. Any drift here makes
    them unreproducible, so the default output is pinned byte-for-byte."""

    def test_the_flag_defaults_to_off(self):
        assert _args(["--instance-id", "local001"]).include_candidate_sql is False

    def test_off_renders_exactly_what_it_renders_today(self):
        text = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION)

        assert text == (
            "[Refiner feedback for CTE totals]\n"
            "Issues:\n"
            "- wrong grain\n"
            "- missing share\n"
            "\nSuggested fix (reference):\ncompute a share, not a raw count\n"
            "\nTests / checks to satisfy:\n"
            "- SELECT COUNT(*) FROM t\n"
            + INSTRUCTION
        )

    def test_the_candidate_sql_is_absent_when_off(self):
        text = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION)

        assert "SUM(n) OVER ()" not in text

    def test_on_without_a_candidate_is_identical_to_off(self):
        """A verdict with no `suggested_fix_sql` must render the same either way, so the
        flag cannot perturb the instances where the refiner offered no SQL."""
        verdict = _verdict(suggested_fix_sql="")

        assert (runner._feedback_text(verdict, "CTE totals", INSTRUCTION, include_candidate_sql=True)
                == runner._feedback_text(verdict, "CTE totals", INSTRUCTION))

    def test_an_ok_verdict_still_yields_no_feedback(self):
        assert runner._feedback_text(
            _verdict(status="ok"), "CTE totals", INSTRUCTION, include_candidate_sql=True
        ) is None


class TestTheCandidateSqlSection:
    def test_the_candidate_sql_is_included(self):
        text = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION, include_candidate_sql=True)

        assert CANDIDATE in text

    def test_it_is_labelled_unvalidated(self):
        """Half of these suggestions do not run, so the agent has to stay sceptical
        rather than copy them wholesale."""
        text = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION, include_candidate_sql=True)

        assert "not validated" in text.lower()

    def test_the_agent_is_told_it_owns_global_consistency(self):
        """This is the whole point: the refiner cannot see downstream CTEs, the agent can.
        Without it the agent reproduces the broken output contracts of stage E."""
        text = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION, include_candidate_sql=True)

        assert "downstream" in text.lower()

    def test_the_existing_sections_are_unchanged(self):
        """Only additive: the prose rationale, issues and tests must read the same."""
        off = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION)
        on = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION, include_candidate_sql=True)

        for kept in ("- wrong grain", "- missing share",
                     "Suggested fix (reference):\ncompute a share, not a raw count",
                     "Tests / checks to satisfy:", "- SELECT COUNT(*) FROM t"):
            assert kept in off and kept in on

    def test_the_original_instruction_survives(self):
        text = runner._feedback_text(_verdict(), "CTE totals", INSTRUCTION, include_candidate_sql=True)

        assert "Revise ONLY the CTE named 'totals'" in text


class TestOptionsAndGuards:
    def test_the_option_travels_to_the_refinement_call(self):
        options = runner._refiner_options(_args(["--instance-id", "local001", "--include-candidate-sql"]))

        assert options["include_candidate_sql"] is True

    def test_it_is_off_in_the_default_options(self):
        assert runner._refiner_options(_args(["--instance-id", "local001"]))["include_candidate_sql"] is False

    def test_combining_it_with_adoption_is_rejected(self):
        """Under `--adopt-refiner-sql` the feedback path is never reached, so the flag
        would be a silent no-op dressed up as an experimental condition."""
        with pytest.raises(ValueError, match="include-candidate-sql"):
            runner._refiner_options(
                _args(["--instance-id", "local001", "--adopt-refiner-sql", "--include-candidate-sql"])
            )


class TestItReachesTheAgent:
    class _Executor:
        def execute(self, sql):
            return ["x"], [(1,)]

        def close(self):
            pass

    def test_the_agent_sees_the_candidate_sql(self, tmp_path, monkeypatch):
        sent = []

        def fake_llm(model, messages, **_k):
            sent.append(messages[-1]["content"])
            return {"choices": [{"message": {"content": "<solution>SELECT 2 AS x</solution>"}}]}

        monkeypatch.setattr(runner, "refiner_run", lambda **_k: _verdict())
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: self._Executor())
        monkeypatch.setattr(runner, "write_csv", lambda *_a, **_k: None)
        monkeypatch.setattr(runner, "llm_completion", fake_llm)
        monkeypatch.setattr(runner, "detect_sql_blocks", lambda _c: [])
        monkeypatch.setattr(runner, "detect_solution", lambda _c: "SELECT 2 AS x")

        runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb", question="q"),
            final_sql="WITH totals AS (\nSELECT 1 AS n FROM t\n)\nSELECT * FROM totals",
            predicted_cte_hint=None,
            engine="sqlite",
            db_path_or_cred=str(tmp_path / "x.sqlite"),
            messages=[],
            out_dir=tmp_path,
            model="gpt-4.1",
            verbose=False,
            include_candidate_sql=True,
        )

        assert sent, "the agent should have been asked to revise"
        assert any("SUM(n) OVER ()" in s for s in sent)

    def test_the_report_records_the_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "refiner_run", lambda **_k: {"status": "ok", "issues": []})
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: self._Executor())
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
            include_candidate_sql=True,
        )

        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        assert report["include_candidate_sql"] is True
