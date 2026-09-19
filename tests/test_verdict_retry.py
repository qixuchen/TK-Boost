"""Validating the forced verdict, and asking again when it does not hold.

Stage 7 of `implementation_plan.md`, resting on the measurement in its §6.8: all 708
fragments of round E reached their verdict through the *forced* path -- the turn budget
ran out, the model was told to decide now, and whatever it returned was taken as-is. That
path executes nothing, so stage 6's self-check (which hangs off the in-loop verdict
branch) never ran once, and 107 suggestions were first discovered to be broken when the
runner substituted them.

So the check belongs after the forced verdict, where the verdicts actually are: substitute
the suggestion into the full query, run it, and on failure hand the refiner the error and
ask again -- same conversation, at most three attempts.

The judgement is execution only. Of the 85 CTE-stage suggestions in round E, 17 were
structurally wrong (multiple CTEs, or a renamed CTE) yet still executed once truncated;
rejecting those on structure would lose them. The 11 that were structurally clean but
failed to execute are why structure alone is not enough either. Structure findings are
therefore only used to explain the failure, never to cause one.
"""

import json
import sqlite3

import pytest

from src.agents import cte_refiner
from src.agents import sql_agent_runner as runner


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


def _verdict(suggested_sql: str = "WITH monthly_totals AS (SELECT 2 AS id)",
             status: str = "issues") -> str:
    return "<verdict_json>" + json.dumps({
        "status": status,
        "issues": ["wrong join"],
        "suggested_fix": "fix the join",
        "suggested_fix_sql": suggested_sql,
        "tests": ["SELECT 1"],
    }) + "</verdict_json>"


PROBE = "<sql>SELECT 1</sql>"


class _ScriptedLLM:
    """Replays `contents`, repeating the last entry once exhausted."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def __call__(self, model, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        idx = min(len(self.calls) - 1, len(self.contents) - 1)
        return {"choices": [{"message": {"content": self.contents[idx]}}]}

    @property
    def prompts(self) -> str:
        """Every message ever sent, flattened -- for asserting what the refiner was told."""
        return "\n".join(str(m.get("content", "")) for call in self.calls for m in call)


class _Validator:
    """Stands in for the runner's closure: rejects with `errors`, one per attempt."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.seen = []

    def __call__(self, suggested_sql):
        self.seen.append(suggested_sql)
        idx = min(len(self.seen) - 1, len(self.errors) - 1)
        return self.errors[idx] if self.errors else None


@pytest.fixture
def db_path(tmp_path) -> str:
    path = tmp_path / "probe.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    return str(path)


def _run(db_path, monkeypatch, *, contents, validate=None, attempts=1, max_turns=1,
         min_probes=8):
    stub = _ScriptedLLM(contents)
    monkeypatch.setattr(cte_refiner, "llm", stub)
    verdict = cte_refiner.run_refiner(
        instance_id="local999",
        db_id="testdb",
        user_query="how many customers",
        cte_text="WITH monthly_totals AS (\nSELECT 1 AS id\n)",
        cte_goal="validate monthly_totals",
        model="gpt-4.1",
        max_turns=max_turns,
        min_required_sql=min_probes,
        verbose=False,
        db_path=db_path,
        validate_fix_sql=validate,
        max_verdict_attempts=attempts,
    )
    return verdict, stub


class TestTheRejectionMessage:
    """What the refiner is told when its suggestion failed. The execution error is always
    there; structure findings are added only to explain it (round E: 62% of suggestions
    returned several CTEs, 53% renamed the CTE)."""

    def test_the_execution_error_is_always_included(self):
        message = cte_refiner._verdict_rejection_message(
            suggested_sql="WITH monthly_totals AS (SELECT speeding FROM t)",
            error="no such column: speeding",
            cte_name_hint="monthly_totals",
        )

        assert "no such column: speeding" in message

    def test_it_says_the_failure_was_in_the_full_query(self):
        """Otherwise the refiner reads it as its own fragment being broken and re-checks
        the fragment, which compiles fine."""
        message = cte_refiner._verdict_rejection_message(
            suggested_sql="WITH monthly_totals AS (SELECT 1 AS id)",
            error="no such column: c",
            cte_name_hint="monthly_totals",
        )

        assert "full query" in message.lower()

    def test_several_ctes_are_called_out_with_the_truncation(self):
        """Only the first CTE survives substitution, so a multi-CTE design is silently
        cut in half -- the refiner cannot know that unless told."""
        message = cte_refiner._verdict_rejection_message(
            suggested_sql=(
                "WITH annual AS (SELECT 1 AS id),\n"
                "ranked AS (SELECT id FROM annual)\n"
                "SELECT * FROM ranked"
            ),
            error="no such column: id",
            cte_name_hint="monthly_totals",
        )

        assert "2" in message
        assert "first" in message.lower()
        assert "single" in message.lower()

    def test_a_renamed_cte_is_called_out_with_both_names(self):
        message = cte_refiner._verdict_rejection_message(
            suggested_sql="WITH annual_counts AS (SELECT 1 AS id)",
            error="no such table: monthly_totals",
            cte_name_hint="monthly_totals",
        )

        assert "annual_counts" in message
        assert "monthly_totals" in message

    def test_a_clean_single_cte_gets_only_the_error(self):
        message = cte_refiner._verdict_rejection_message(
            suggested_sql="WITH monthly_totals AS (SELECT 1 AS id)",
            error="no such column: id",
            cte_name_hint="monthly_totals",
        )

        assert "no such column: id" in message
        assert "first" not in message.lower()
        assert "monthly_totals" not in message.replace("no such column: id", "")


class TestTheForcedVerdictIsValidated:
    """The gap stage 6 left open: the forced path took whatever came back."""

    def test_the_validator_sees_the_forced_suggestion(self, db_path, monkeypatch):
        validate = _Validator(None)

        _run(db_path, monkeypatch, contents=[_verdict()], validate=validate, attempts=3)

        assert validate.seen == ["WITH monthly_totals AS (SELECT 2 AS id)"]

    def test_a_passing_verdict_is_returned_without_another_attempt(self, db_path, monkeypatch):
        validate = _Validator(None)

        verdict, stub = _run(db_path, monkeypatch, contents=[_verdict()],
                             validate=validate, attempts=3)

        assert verdict["status"] == "issues"
        assert verdict["verdict_attempts"] == 1
        assert len(validate.seen) == 1
        assert len(stub.calls) == 2, "turn 1 plus the forced verdict, nothing more"

    def test_an_ok_verdict_is_never_validated(self, db_path, monkeypatch):
        """Nothing will be substituted, so there is nothing to check."""
        validate = _Validator("must not be called")

        _run(db_path, monkeypatch, contents=[_verdict(status="ok")],
             validate=validate, attempts=3)

        assert validate.seen == []

    def test_an_empty_suggestion_is_never_validated(self, db_path, monkeypatch):
        validate = _Validator("must not be called")

        _run(db_path, monkeypatch, contents=[_verdict(suggested_sql="")],
             validate=validate, attempts=3)

        assert validate.seen == []


class TestTheRetry:
    def test_a_rejection_buys_another_attempt(self, db_path, monkeypatch):
        validate = _Validator("no such column: speeding", None)

        verdict, stub = _run(db_path, monkeypatch, contents=[_verdict()],
                             validate=validate, attempts=3)

        assert len(validate.seen) == 2
        assert verdict["verdict_attempts"] == 2

    def test_the_error_is_handed_back_to_the_refiner(self, db_path, monkeypatch):
        _, stub = _run(db_path, monkeypatch, contents=[_verdict()],
                       validate=_Validator("no such column: speeding"), attempts=3)

        assert "no such column: speeding" in stub.prompts

    def test_it_continues_the_same_conversation(self, db_path, monkeypatch):
        """The refiner has just probed the schema; restarting would throw that away and
        pay for it again."""
        _, stub = _run(db_path, monkeypatch, contents=[PROBE, _verdict()],
                       validate=_Validator("no such column: speeding"), attempts=2,
                       max_turns=2)

        last_call = stub.calls[-1]
        assert any("SQL_RESULT_TABLE" in str(m.get("content", "")) for m in last_call), \
            "the earlier probe result must still be in context"

    def test_the_probe_count_is_not_reset(self, db_path, monkeypatch):
        """Clearing it would make the minimum-probes gate bite again and force three fresh
        probes; carrying it means the refiner may probe more but is not made to.

        Two attempts of `probe, verdict, forced verdict`, so the gate reports the running
        total each time it fires."""
        script = [PROBE, _verdict(), _verdict()] * 2
        _, stub = _run(db_path, monkeypatch, contents=script,
                       validate=_Validator("boom"), attempts=2, max_turns=2,
                       min_probes=3)

        assert "more probes (1/3)" in stub.prompts, "first attempt probed once"
        assert "more probes (2/3)" in stub.prompts, \
            "the second attempt must count the first attempt's probe too"

    def test_it_gives_up_after_the_third_attempt(self, db_path, monkeypatch):
        validate = _Validator("still broken")

        verdict, _ = _run(db_path, monkeypatch, contents=[_verdict()],
                          validate=validate, attempts=3)

        assert len(validate.seen) == 3
        assert verdict["verdict_attempts"] == 3

    def test_giving_up_is_recorded_rather_than_hidden(self, db_path, monkeypatch):
        """The runner keeps the original SQL when a suggestion does not run, so the
        artifact is the only place the three failures are visible."""
        verdict, _ = _run(db_path, monkeypatch, contents=[_verdict()],
                          validate=_Validator("still broken"), attempts=3)

        assert verdict["verdict_validated"] is False
        assert verdict["verdict_validation_errors"] == ["still broken"] * 3

    def test_a_validated_verdict_says_so(self, db_path, monkeypatch):
        verdict, _ = _run(db_path, monkeypatch, contents=[_verdict()],
                          validate=_Validator("broken once", None), attempts=3)

        assert verdict["verdict_validated"] is True
        assert verdict["verdict_validation_errors"] == ["broken once"]

    def test_one_attempt_means_no_retry(self, db_path, monkeypatch):
        """The flag is off by default, which must leave round E's behaviour in place."""
        validate = _Validator("broken")

        verdict, _ = _run(db_path, monkeypatch, contents=[_verdict()],
                          validate=validate, attempts=1)

        assert len(validate.seen) == 1
        assert verdict["verdict_attempts"] == 1


class TestRejectionsInsideTheTurnLoopCountToo:
    """Measured on local018: the retry made the refiner compile-test its CTE, which set
    `harness_executed` and so opened the in-loop verdict branch that round E never reached.
    Stage 6's check then rejected one more suggestion there -- invisibly, because only the
    forced-path rejection was being recorded and the trace kept no reason. Reconstructing
    it afterwards took re-executing each suggestion by hand.
    """

    @pytest.fixture
    def db_path(self, tmp_path) -> str:
        """Carries a view named after the CTE, so the harness probe can succeed."""
        path = tmp_path / "harness.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("CREATE VIEW monthly_totals AS SELECT 1 AS id")
        conn.commit()
        conn.close()
        return str(path)

    HARNESS_PROBE = "<sql>SELECT * FROM monthly_totals LIMIT 10</sql>"

    def _run(self, db_path, monkeypatch, *, trace_path=None):
        stub = _ScriptedLLM([self.HARNESS_PROBE, _verdict(), _verdict()])
        monkeypatch.setattr(cte_refiner, "llm", stub)
        verdict = cte_refiner.run_refiner(
            instance_id="local999", db_id="testdb", user_query="q",
            cte_text="WITH monthly_totals AS (\nSELECT 1 AS id\n)",
            cte_goal="g", model="gpt-4.1", max_turns=3, min_required_sql=1,
            verbose=False, db_path=db_path, validate_fix_sql=_Validator("boom"),
            max_verdict_attempts=1,
            trace_output_path=str(trace_path) if trace_path else None,
        )
        return verdict, stub

    def test_the_in_loop_branch_is_actually_reached(self, db_path, monkeypatch):
        """Guards the setup: without the harness probe this test would measure nothing."""
        _, stub = self._run(db_path, monkeypatch)

        assert not any("Before verdict, do:" in str(m.get("content", ""))
                       for call in stub.calls for m in call), \
            "the harness gate should be satisfied, so the verdict must not be turned away"
        assert any("Revise <verdict_json>" in str(m.get("content", ""))
                   for call in stub.calls for m in call), \
            "an in-loop verdict should have been rejected and sent back"

    def test_every_rejection_is_recorded_not_just_the_forced_one(self, db_path, monkeypatch):
        verdict, _ = self._run(db_path, monkeypatch)

        assert verdict["verdict_attempts"] == 1
        assert len(verdict["verdict_validation_errors"]) == 3, \
            "two in-loop rejections plus the forced one"

    def test_each_rejection_is_only_counted_once(self, db_path, monkeypatch):
        """The forced-path check must not re-record what the recorder already logged."""
        verdict, _ = self._run(db_path, monkeypatch)

        assert verdict["verdict_validation_errors"] == ["boom"] * 3

    def test_the_trace_keeps_the_reason_for_each(self, db_path, monkeypatch, tmp_path):
        trace_path = tmp_path / "trace.txt"

        self._run(db_path, monkeypatch, trace_path=trace_path)

        trace = trace_path.read_text()
        assert trace.count("VERDICT REJECTED") == 3
        assert "boom" in trace


class TestWithoutAValidatorNothingChanges:
    """Rounds A-F must stay reproducible."""

    def test_no_retry_happens(self, db_path, monkeypatch):
        _, stub = _run(db_path, monkeypatch, contents=[_verdict()], attempts=3)

        assert len(stub.calls) == 2, "turn 1 plus the forced verdict"

    def test_no_audit_fields_are_added(self, db_path, monkeypatch):
        verdict, _ = _run(db_path, monkeypatch, contents=[_verdict()], attempts=3)

        assert "verdict_attempts" not in verdict
        assert "verdict_validated" not in verdict

    def test_the_verdict_itself_is_untouched(self, db_path, monkeypatch):
        verdict, _ = _run(db_path, monkeypatch, contents=[_verdict()])

        assert verdict["status"] == "issues"
        assert verdict["suggested_fix_sql"] == "WITH monthly_totals AS (SELECT 2 AS id)"


class TestRunnerWiring:
    def test_it_defaults_to_a_single_attempt(self):
        assert _args(["--instance-id", "local001"]).verdict_attempts == 1

    def test_it_is_carried_in_the_refiner_options(self):
        options = runner._refiner_options(_args([
            "--instance-id", "local001", "--adopt-refiner-sql",
            "--validate-fix-in-context", "--verdict-attempts", "3",
        ]))

        assert options["verdict_attempts"] == 3

    def test_retrying_requires_the_validator(self):
        """Without it there is nothing to validate against, so a retry has no trigger."""
        with pytest.raises(ValueError, match="verdict-attempts"):
            runner._refiner_options(_args([
                "--instance-id", "local001", "--adopt-refiner-sql",
                "--verdict-attempts", "3",
            ]))

    def test_a_single_attempt_needs_no_validator(self):
        options = runner._refiner_options(
            _args(["--instance-id", "local001", "--verdict-attempts", "1"])
        )

        assert options["verdict_attempts"] == 1

    def test_fewer_than_one_attempt_is_rejected(self):
        with pytest.raises(ValueError, match="verdict-attempts"):
            runner._refiner_options(_args(["--instance-id", "local001",
                                           "--verdict-attempts", "0"]))


TWO_CTE_SQL = """WITH totals AS (
SELECT 1 AS n FROM t
),
shares AS (
SELECT n FROM totals
)
SELECT * FROM shares"""


class _Executor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql):
        self.calls.append(sql)
        if "broken" in sql.lower():
            raise RuntimeError("no such column: broken")
        return ["n"], [(1,)]

    def close(self):
        pass


class TestItReachesTheRefiner:
    @pytest.fixture
    def refiner_calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(runner, "refiner_run",
                            lambda **kw: calls.append(kw) or {"status": "ok", "issues": []})
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: _Executor([]))
        monkeypatch.setattr(runner, "write_csv", lambda *_a, **_k: None)
        return calls

    def _refine(self, tmp_path, **kw):
        return runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb", question="q"),
            final_sql=TWO_CTE_SQL, predicted_cte_hint=None, engine="sqlite",
            db_path_or_cred=str(tmp_path / "x.sqlite"), messages=[], out_dir=tmp_path,
            model="gpt-4.1", verbose=False, **kw,
        )

    def test_the_budget_is_passed_through(self, tmp_path, refiner_calls):
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True,
                     verdict_attempts=3)

        assert refiner_calls[0]["max_verdict_attempts"] == 3

    def test_the_final_select_stage_gets_it_too(self, tmp_path, refiner_calls):
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True,
                     verdict_attempts=3)

        assert refiner_calls[-1]["max_verdict_attempts"] == 3

    def test_the_final_select_stage_is_given_a_validator(self, tmp_path, refiner_calls):
        """Without one the budget would be a no-op there: the retry has no trigger."""
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True,
                     verdict_attempts=3)

        assert callable(refiner_calls[-1]["validate_fix_sql"])

    def test_the_final_select_suggestion_is_judged_as_written(self, tmp_path, refiner_calls):
        """At that stage the refiner saw the complete query, so its suggestion *is* the
        complete query and nothing needs reassembling."""
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True,
                     verdict_attempts=3)
        validate = refiner_calls[-1]["validate_fix_sql"]

        assert validate("SELECT 1 AS n") is None
        assert "broken" in (validate("SELECT broken AS n") or "")

    def test_the_default_stays_at_one(self, tmp_path, refiner_calls):
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True)

        assert refiner_calls[0].get("max_verdict_attempts", 1) == 1

    def test_the_report_records_it(self, tmp_path, monkeypatch, refiner_calls):
        monkeypatch.setattr(runner, "_retrieve_rules_for", lambda **_k: ([], []))

        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True,
                     verdict_attempts=3, tkstore_path=str(tmp_path / "store.csv"))

        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        assert report["verdict_attempts"] == 3


class TestThePipelineScript:
    """The orchestrator validates the combination before the agent step, because the agent
    step runs for hours and the arms come after it."""

    @pytest.fixture
    def split_file(self, tmp_path):
        path = tmp_path / "split.txt"
        path.write_text("local001\nlocal002\n")
        return path

    def _run(self, split_file, tmp_path, *extra):
        import subprocess
        import sys
        return subprocess.run(
            [sys.executable, "scripts/run_pipeline.py", "--dry-run",
             "--split", str(split_file), "--out-prefix", str(tmp_path / "out"),
             "--tkstore", "tkstore/tkstore_sqlite.csv", *extra],
            capture_output=True, text=True,
        )

    def test_the_budget_appears_in_both_arm_commands(self, split_file, tmp_path):
        done = self._run(split_file, tmp_path, "--adopt-refiner-sql",
                         "--validate-fix-in-context", "--verdict-attempts", "3")

        assert done.returncode == 0, done.stderr
        assert done.stdout.count("--verdict-attempts 3") == 2

    def test_it_refuses_the_budget_without_the_validator_before_running(self, split_file, tmp_path):
        done = self._run(split_file, tmp_path, "--verdict-attempts", "3")

        assert done.returncode != 0
        assert "requires --validate-fix-in-context" in done.stderr
