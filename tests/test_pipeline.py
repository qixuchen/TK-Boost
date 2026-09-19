"""Planning the three-arm comparison.

The comparison rests on one invariant: every arm is refined from the *same* agent
output, so the only difference between arm `refonly` and arm `tk` is the store. The
planner exists to make that hard to get wrong by hand, and
`verify_shared_agent_output` checks it held.
"""

from pathlib import Path

import pytest

from src.utils import pipeline


IDS = ["local001", "local002", "local003", "local004", "local005"]


@pytest.fixture
def split_file(tmp_path):
    path = tmp_path / "split.txt"
    path.write_text("# a comment\n" + "\n".join(IDS) + "\n", encoding="utf-8")
    return path


def _argv_of(steps, name):
    return next(s.argv for s in steps if s.name == name)


class TestPlanBatches:
    def test_without_a_batch_size_there_is_one_batch(self, split_file, tmp_path):
        batches = pipeline.plan_batches(split_file, tmp_path / "test")

        assert len(batches) == 1
        assert batches[0].offset == 0
        assert batches[0].limit is None

    def test_batches_tile_the_split_without_gaps_or_overlap(self, split_file, tmp_path):
        batches = pipeline.plan_batches(split_file, tmp_path / "test", batch_size=2)

        assert [(b.offset, b.limit) for b in batches] == [(0, 2), (2, 2), (4, 1)]

    def test_the_last_batch_states_its_real_size(self, split_file, tmp_path):
        """An honest `--split-limit` makes the logged command reproducible on its own."""
        batches = pipeline.plan_batches(split_file, tmp_path / "test", batch_size=3)

        assert [(b.offset, b.limit) for b in batches] == [(0, 3), (3, 2)]

    def test_a_batch_size_covering_everything_yields_one_batch(self, split_file, tmp_path):
        batches = pipeline.plan_batches(split_file, tmp_path / "test", batch_size=99)

        assert [(b.offset, b.limit) for b in batches] == [(0, 5)]

    def test_unbatched_directories_have_no_offset_suffix(self, split_file, tmp_path):
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]

        assert batch.agent_dir == tmp_path / "test_agent"
        assert batch.refonly_dir == tmp_path / "test_refonly"
        assert batch.tk_dir == tmp_path / "test_tk"

    def test_batched_directories_are_distinct_per_batch(self, split_file, tmp_path):
        batches = pipeline.plan_batches(split_file, tmp_path / "test", batch_size=2)

        assert batches[0].agent_dir == tmp_path / "test_b0_agent"
        assert batches[1].agent_dir == tmp_path / "test_b2_agent"
        assert len({b.agent_dir for b in batches}) == 3

    def test_a_non_positive_batch_size_is_an_error(self, split_file, tmp_path):
        with pytest.raises(ValueError, match="positive"):
            pipeline.plan_batches(split_file, tmp_path / "test", batch_size=0)


class TestPlanSteps:
    @pytest.fixture
    def steps(self, split_file, tmp_path):
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        return pipeline.plan_steps(batch, split_file, tkstore="tkstore/tkstore_sqlite.csv")

    def test_the_agent_runs_before_either_arm(self, steps):
        assert [s.name for s in steps] == ["agent", "arm_refonly", "arm_tk"]

    def test_in_context_validation_is_not_passed_unless_asked(self, steps):
        for step in steps:
            assert "--validate-fix-in-context" not in step.argv, step.name

    def test_in_context_validation_reaches_both_arms_but_not_the_agent(self, split_file, tmp_path):
        """It changes how the refiner judges its own fix, which both arms must share."""
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        steps = pipeline.plan_steps(batch, split_file, tkstore="tkstore/tkstore_sqlite.csv",
                                    adopt_refiner_sql=True, validate_fix_in_context=True)

        assert "--validate-fix-in-context" not in _argv_of(steps, "agent")
        for name in ("arm_refonly", "arm_tk"):
            assert "--validate-fix-in-context" in _argv_of(steps, name), name

    def test_the_verdict_budget_is_not_passed_unless_asked(self, steps):
        """Omitting it leaves the runner's default of one attempt, which is round E."""
        for step in steps:
            assert "--verdict-attempts" not in step.argv, step.name

    def test_the_verdict_budget_reaches_both_arms_but_not_the_agent(self, split_file, tmp_path):
        """It changes how many chances a suggestion gets, so a one-sided setting would make
        the pairing measure the retry rather than knowledge."""
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        steps = pipeline.plan_steps(batch, split_file, tkstore="tkstore/tkstore_sqlite.csv",
                                    adopt_refiner_sql=True, validate_fix_in_context=True,
                                    verdict_attempts=3)

        assert "--verdict-attempts" not in _argv_of(steps, "agent")
        for name in ("arm_refonly", "arm_tk"):
            argv = _argv_of(steps, name)
            assert "--verdict-attempts" in argv, name
            assert argv[argv.index("--verdict-attempts") + 1] == "3", name

    def test_the_candidate_sql_flag_is_not_passed_unless_asked(self, steps):
        for step in steps:
            assert "--include-candidate-sql" not in step.argv, step.name

    def test_the_candidate_sql_flag_reaches_both_arms_but_not_the_agent(self, split_file, tmp_path):
        """It changes how a verdict is rendered, which both arms must share or the pairing
        measures the rendering rather than knowledge."""
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        steps = pipeline.plan_steps(batch, split_file, tkstore="tkstore/tkstore_sqlite.csv",
                                    include_candidate_sql=True)

        assert "--include-candidate-sql" not in _argv_of(steps, "agent")
        for name in ("arm_refonly", "arm_tk"):
            assert "--include-candidate-sql" in _argv_of(steps, name), name

    def test_adoption_is_not_passed_unless_asked(self, steps):
        for step in steps:
            assert "--adopt-refiner-sql" not in step.argv, step.name

    def test_adoption_reaches_both_arms_but_not_the_agent(self, split_file, tmp_path):
        """It changes how the refiner's verdict is applied, which both arms must share
        or the pairing measures adoption rather than knowledge."""
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        steps = pipeline.plan_steps(batch, split_file, tkstore="tkstore/tkstore_sqlite.csv",
                                    adopt_refiner_sql=True)

        assert "--adopt-refiner-sql" not in _argv_of(steps, "agent")
        for name in ("arm_refonly", "arm_tk"):
            assert "--adopt-refiner-sql" in _argv_of(steps, name), name

    def test_the_refiner_budget_is_not_passed_unless_asked(self, steps):
        """Omitting it keeps the runner's default, so the reference run stays reproducible."""
        for step in steps:
            assert "--refiner-turns" not in step.argv, step.name
            assert "--refiner-min-probes" not in step.argv, step.name

    def test_the_refiner_budget_reaches_both_arms_but_not_the_agent(self, split_file, tmp_path):
        """The agent step has no refiner, so the flags would be meaningless noise there."""
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        steps = pipeline.plan_steps(batch, split_file, tkstore="tkstore/tkstore_sqlite.csv",
                                    refiner_turns=5, refiner_min_probes=3)

        agent = _argv_of(steps, "agent")
        assert "--refiner-turns" not in agent

        for name in ("arm_refonly", "arm_tk"):
            argv = _argv_of(steps, name)
            assert argv[argv.index("--refiner-turns") + 1] == "5", name
            assert argv[argv.index("--refiner-min-probes") + 1] == "3", name

    def test_every_step_runs_unbuffered(self, steps):
        """The child does the progress printing. Block-buffered through a `tee` pipe it
        withholds output for kilobytes at a time, which reads as a hung run."""
        for step in steps:
            assert "-u" in step.argv, step.name

    def test_the_agent_step_is_a_bare_run(self, steps, split_file, tmp_path):
        argv = _argv_of(steps, "agent")

        assert argv[:4] == [pipeline.PYTHON, "-u", "-m", "src.agents.sql_agent_runner"]
        assert "--split" in argv and str(split_file) in argv
        assert "--out-base" in argv and str(tmp_path / "test_agent") in argv
        assert "--tkstore" not in argv
        assert "--refine-cte" not in argv
        assert "--refine-output" not in argv

    def test_arm_refonly_refines_the_shared_output_without_a_store(self, steps, tmp_path):
        argv = _argv_of(steps, "arm_refonly")

        assert argv[argv.index("--refine-output") + 1] == str(tmp_path / "test_agent")
        assert argv[argv.index("--refine-output-dir") + 1] == str(tmp_path / "test_refonly")
        assert "--tkstore" not in argv

    def test_arm_tk_refines_the_same_output_with_the_store(self, steps, tmp_path):
        argv = _argv_of(steps, "arm_tk")

        assert argv[argv.index("--refine-output") + 1] == str(tmp_path / "test_agent")
        assert argv[argv.index("--refine-output-dir") + 1] == str(tmp_path / "test_tk")
        assert argv[argv.index("--tkstore") + 1] == "tkstore/tkstore_sqlite.csv"

    def test_the_arms_read_the_same_agent_directory(self, steps):
        a = _argv_of(steps, "arm_refonly")
        b = _argv_of(steps, "arm_tk")

        assert a[a.index("--refine-output") + 1] == b[b.index("--refine-output") + 1]

    def test_batching_flags_go_only_to_the_agent_step(self, split_file, tmp_path):
        """The arms walk their directory; `--split` is inert there and would mislead."""
        batch = pipeline.plan_batches(split_file, tmp_path / "test", batch_size=2)[1]
        steps = pipeline.plan_steps(batch, split_file, tkstore="s.csv")

        agent = _argv_of(steps, "agent")
        assert agent[agent.index("--split-offset") + 1] == "2"
        assert agent[agent.index("--split-limit") + 1] == "2"
        for name in ("arm_refonly", "arm_tk"):
            assert "--split" not in _argv_of(steps, name)
            assert "--split-offset" not in _argv_of(steps, name)

    def test_the_model_reaches_every_step(self, split_file, tmp_path):
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        steps = pipeline.plan_steps(batch, split_file, tkstore="s.csv", model="gpt-4.1")

        for s in steps:
            assert s.argv[s.argv.index("--model") + 1] == "gpt-4.1"

    def test_retrieval_options_reach_only_the_knowledge_arm(self, split_file, tmp_path):
        """`--tkstore` is rejected without a refinement flag, so these belong to arm_tk."""
        batch = pipeline.plan_batches(split_file, tmp_path / "test")[0]
        steps = pipeline.plan_steps(
            batch, split_file, tkstore="s.csv", filter_model="gpt-4o", use_llm_filtering=False
        )

        tk = _argv_of(steps, "arm_tk")
        assert tk[tk.index("--filter-model") + 1] == "gpt-4o"
        assert "--no-llm-filtering" in tk
        for name in ("agent", "arm_refonly"):
            assert "--filter-model" not in _argv_of(steps, name)
            assert "--no-llm-filtering" not in _argv_of(steps, name)

    def test_llm_filtering_is_on_by_default(self, steps):
        assert "--no-llm-filtering" not in _argv_of(steps, "arm_tk")


def _write_instance(base, name, sql):
    d = base / name
    d.mkdir(parents=True)
    (d / "execution_query.sql").write_text(sql, encoding="utf-8")
    return d


class TestVerifySharedAgentOutput:
    """If this ever reports a mismatch, `delta_knowledge` is not a paired comparison."""

    def test_identical_starting_sql_is_clean(self, tmp_path):
        agent, arm = tmp_path / "agent", tmp_path / "arm"
        _write_instance(agent, "local001_20260101_000000", "SELECT 1")
        _write_instance(arm, "local001_20260101_000000", "SELECT 1")

        assert pipeline.verify_shared_agent_output(agent, arm) == []

    def test_a_differing_starting_sql_is_reported(self, tmp_path):
        agent, arm = tmp_path / "agent", tmp_path / "arm"
        _write_instance(agent, "local001_20260101_000000", "SELECT 1")
        _write_instance(arm, "local001_20260101_000000", "SELECT 2")

        assert pipeline.verify_shared_agent_output(agent, arm) == ["local001_20260101_000000"]

    def test_an_instance_missing_from_the_arm_is_reported(self, tmp_path):
        agent, arm = tmp_path / "agent", tmp_path / "arm"
        _write_instance(agent, "local001_20260101_000000", "SELECT 1")
        _write_instance(agent, "local002_20260101_000000", "SELECT 2")
        _write_instance(arm, "local001_20260101_000000", "SELECT 1")

        assert pipeline.verify_shared_agent_output(agent, arm) == ["local002_20260101_000000"]

    def test_refinement_artifacts_do_not_count_as_a_mismatch(self, tmp_path):
        """Revisions land in `execution_query_after_*.sql`; the original must be untouched."""
        agent, arm = tmp_path / "agent", tmp_path / "arm"
        _write_instance(agent, "local001_20260101_000000", "SELECT 1")
        d = _write_instance(arm, "local001_20260101_000000", "SELECT 1")
        (d / "execution_query_after_x.sql").write_text("SELECT 99", encoding="utf-8")
        (d / "refinement_complete.marker").write_text("done", encoding="utf-8")

        assert pipeline.verify_shared_agent_output(agent, arm) == []

    def test_an_instance_the_agent_never_finished_is_ignored(self, tmp_path):
        """A failed agent instance leaves an empty `execution_query.sql` and is not
        synced into the arms. There is no starting SQL to pair on, so reporting it would
        hard-fail the pipeline over an instance nobody could have refined."""
        agent, arm = tmp_path / "agent", tmp_path / "arm"
        _write_instance(agent, "local001_20260101_000000", "SELECT 1")
        _write_instance(agent, "local002_20260101_000000", "")
        _write_instance(arm, "local001_20260101_000000", "SELECT 1")

        assert pipeline.verify_shared_agent_output(agent, arm) == []

    def test_a_missing_arm_directory_is_an_error(self, tmp_path):
        agent = tmp_path / "agent"
        _write_instance(agent, "local001_20260101_000000", "SELECT 1")

        with pytest.raises(FileNotFoundError):
            pipeline.verify_shared_agent_output(agent, tmp_path / "nope")


class TestRunSteps:
    @pytest.fixture
    def steps(self):
        return [
            pipeline.Step(name="agent", argv=["python", "agent"]),
            pipeline.Step(name="arm_refonly", argv=["python", "a"]),
            pipeline.Step(name="arm_tk", argv=["python", "b"]),
        ]

    def test_steps_run_in_order(self, steps):
        seen = []
        code = pipeline.run_steps(steps, run=lambda argv: seen.append(argv) or 0)

        assert code == 0
        assert [a[1] for a in seen] == ["agent", "a", "b"]

    def test_a_dry_run_executes_nothing(self, steps):
        code = pipeline.run_steps(steps, run=lambda argv: pytest.fail("must not run"), dry_run=True)

        assert code == 0

    def test_a_failing_step_stops_the_pipeline(self, steps):
        """Refining a half-finished agent directory would silently shrink the sample."""
        seen = []

        def run(argv):
            seen.append(argv)
            return 1 if argv[1] == "agent" else 0

        code = pipeline.run_steps(steps, run=run)

        assert code == 1
        assert len(seen) == 1
