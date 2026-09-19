"""Making the refiner's probing budget configurable.

Two upstream entry points disagree: `tkboost.sql()` gives each fragment 5 probing
turns (`turns_per_cte` floors at 5 with the default `max_turns=10`), while
`sql_agent_runner.py` has always hardcoded 25. The reported gains come from the
5-turn path, so reproducing them means being able to run both.

The turn budget cannot be lowered on its own. `run_refiner` refuses to accept its own
verdict until `min_required_sql` probes have run, one per turn, and that defaults to
8 -- so `--refiner-turns 5` alone can never reach a verdict. It would burn every turn
being told `more probes (n/8)`, fall through to the forced-verdict path, and in the
worst case return the `no_verdict` fallback, whose status is `issues` and therefore
*triggers a rewrite* on a fragment nobody diagnosed. Hence the guard below.
"""

import pytest

from src.agents import cte_refiner
from src.agents import sql_agent_runner as runner


TWO_CTE_SQL = """WITH customer_totals AS (
SELECT 1 AS x FROM t1
),
monthly_revenue AS (
SELECT 2 AS y FROM t2
)
SELECT * FROM monthly_revenue"""


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


class FakeExecutor:
    def execute(self, sql):
        return ["x"], [(1,)]

    def close(self):
        pass


@pytest.fixture
def refiner_calls(monkeypatch):
    calls = []

    def fake_refiner_run(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "issues": []}

    monkeypatch.setattr(runner, "refiner_run", fake_refiner_run)
    monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: FakeExecutor())
    return calls


class TestParserDefaults:
    def test_the_current_budget_is_the_default(self):
        """Changing the default would make the 86-instance reference run unreproducible."""
        args = _args(["--instance-id", "local001"])

        assert args.refiner_turns == 25

    def test_the_probe_minimum_defers_to_the_refiner(self):
        """None means 'whatever run_refiner uses', which keeps behaviour byte-identical."""
        args = _args(["--instance-id", "local001"])

        assert args.refiner_min_probes is None

    def test_both_can_be_set(self):
        args = _args(["--instance-id", "local001", "--refiner-turns", "5",
                      "--refiner-min-probes", "3"])

        assert (args.refiner_turns, args.refiner_min_probes) == (5, 3)


class TestRefinerOptions:
    def test_defaults_reproduce_the_reference_run(self):
        options = runner._refiner_options(_args(["--instance-id", "local001"]))

        assert options == {"refiner_turns": 25, "refiner_min_probes": None,
                           "adopt_refiner_sql": False}

    def test_the_upstream_configuration_passes_through(self):
        options = runner._refiner_options(
            _args(["--instance-id", "local001", "--refiner-turns", "5",
                   "--refiner-min-probes", "3"])
        )

        assert options == {"refiner_turns": 5, "refiner_min_probes": 3,
                           "adopt_refiner_sql": False}

    def test_a_probe_minimum_the_budget_cannot_reach_is_rejected(self):
        """`--refiner-turns 5` with the default minimum of 8 is the trap this guards."""
        with pytest.raises(ValueError, match="refiner-min-probes"):
            runner._refiner_options(_args(["--instance-id", "local001", "--refiner-turns", "5"]))

    def test_an_explicit_minimum_at_the_budget_is_rejected_too(self):
        """One turn has to be left for the verdict itself."""
        with pytest.raises(ValueError, match="refiner-min-probes"):
            runner._refiner_options(
                _args(["--instance-id", "local001", "--refiner-turns", "5",
                       "--refiner-min-probes", "5"])
            )

    def test_a_non_positive_budget_is_rejected(self):
        with pytest.raises(ValueError, match="refiner-turns"):
            runner._refiner_options(_args(["--instance-id", "local001", "--refiner-turns", "0"]))

    def test_the_refiner_default_is_what_the_guard_compares_against(self):
        """If run_refiner's default changes, the guard must follow it rather than a copy."""
        assert cte_refiner.DEFAULT_MIN_PROBES == 8


class TestBudgetReachesTheRefiner:
    def _refine(self, out_dir, **budget):
        runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb", question="q"),
            final_sql=TWO_CTE_SQL,
            predicted_cte_hint=None,
            engine="sqlite",
            db_path_or_cred=str(out_dir / "x.sqlite"),
            messages=[],
            out_dir=out_dir,
            model="gpt-4.1",
            verbose=False,
            **budget,
        )

    def test_every_fragment_gets_the_configured_budget(self, tmp_path, refiner_calls):
        self._refine(tmp_path, refiner_turns=5, refiner_min_probes=3)

        assert refiner_calls, "the refiner should have been called"
        assert {c["max_turns"] for c in refiner_calls} == {5}
        assert {c["min_required_sql"] for c in refiner_calls} == {3}

    def test_the_default_keeps_the_reference_behaviour(self, tmp_path, refiner_calls):
        self._refine(tmp_path)

        assert {c["max_turns"] for c in refiner_calls} == {25}
        assert {c["min_required_sql"] for c in refiner_calls} == {None}


class TestRefineOutputPassthrough:
    """The ablation runs through --refine-output, so the budget has to reach that path."""

    def test_the_budget_reaches_the_refinement_only_path(self, tmp_path, monkeypatch):
        src = tmp_path / "agent"
        inst_dir = src / "local001_20260101_000000"
        inst_dir.mkdir(parents=True)
        (inst_dir / "execution_query.sql").write_text("SELECT 1", encoding="utf-8")

        seen = {}

        def fake_refine(**kwargs):
            seen.update(kwargs)
            return "SELECT 1", None

        monkeypatch.setattr(runner, "perform_refinement_and_revision", fake_refine)
        monkeypatch.setattr(
            runner, "load_instances_from_jsonl",
            lambda _p: [runner.Instance(instance_id="local001", db="testdb", question="q")],
        )
        monkeypatch.setattr(runner, "resolve_sqlite_db_path", lambda *_a, **_k: str(tmp_path / "db.sqlite"))
        monkeypatch.setattr(runner, "get_system_prompt", lambda *_a, **_k: "sys")
        monkeypatch.setattr(runner, "build_user_message", lambda *_a, **_k: "user")
        monkeypatch.setattr(runner, "load_external_knowledge", lambda *_a, **_k: None)
        monkeypatch.setattr(runner, "_choose_and_mark_final_artifacts", lambda *_a, **_k: None)

        args = _args(["--refine-output", str(src), "--refine-output-dir", str(tmp_path / "arm"),
                      "--refiner-turns", "5", "--refiner-min-probes", "3"])
        runner.run_refinement_on_existing_outputs(args)

        assert seen.get("refiner_turns") == 5
        assert seen.get("refiner_min_probes") == 3

    def test_an_unreachable_budget_is_rejected_before_any_work(self, tmp_path, monkeypatch, capsys):
        """Left to the per-instance body, the ValueError is swallowed by its
        `except Exception` and every instance is merely counted as failed -- which is
        indistinguishable from the refiner having genuinely struggled."""
        monkeypatch.setattr(
            "sys.argv",
            ["sql_agent_runner", "--refine-output", str(tmp_path / "agent"), "--refiner-turns", "5"],
        )
        monkeypatch.setattr(
            runner, "run_refinement_on_existing_outputs",
            lambda _a: pytest.fail("must not start refining with an unreachable budget"),
        )

        with pytest.raises(SystemExit) as exit_info:
            runner.main()

        assert exit_info.value.code != 0
        assert "refiner-min-probes" in capsys.readouterr().err
