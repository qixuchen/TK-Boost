"""Pointing the refiner's own self-check at the whole query.

Stage 6 of `implementation_plan.md`. The refiner already retries when its
`suggested_fix_sql` fails to compile (`cte_refiner.py:570-590`: on failure it appends the
error to its own conversation, clears the verdict and continues). But it compiles the
fragment *in isolation*, so a rewrite that renames the CTE's output columns passes there
and only breaks once substituted back -- 33 of the 108 suggestions blocked in round E were
exactly that.

Two coupled changes fix it: show the refiner what reads its output (`[DOWNSTREAM]`), and
let the caller decide whether a fix is acceptable, since only the caller can reassemble
the query (`previous_ctes` is a display format, one `WITH` per block, not concatenable).
The existing retry loop then does the rest.
"""

import sqlite3

import pytest

from src.agents import cte_refiner
from src.agents import sql_agent_runner as runner


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


class TestDownstreamSection:
    """Symmetric with the existing `[PREVIOUS_CTES]`: what comes after, verbatim."""

    def test_it_is_rendered_when_given(self):
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
            downstream="WITH b AS (SELECT x FROM a)\nSELECT * FROM b",
        )

        assert "[DOWNSTREAM]" in payload
        assert "SELECT x FROM a" in payload

    def test_it_is_absent_when_empty(self):
        """The last CTE has nothing after it, and an instance with no CTEs never gets here."""
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
            downstream="",
        )

        assert "[DOWNSTREAM]" not in payload

    def test_it_precedes_the_target_cte(self):
        """`[CTE]` stays next to `[CTE_GOAL]` so what to change sits beside how."""
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
            downstream="SELECT * FROM a",
        )

        assert payload.index("[DOWNSTREAM]") < payload.index("[CTE]")

    def test_the_constraint_instruction_comes_with_it(self):
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
            downstream="SELECT * FROM a",
        )

        assert "preserve the output column names" in payload

    def test_no_constraint_instruction_without_downstream(self):
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
        )

        assert "preserve the output column names" not in payload

    def test_the_default_payload_is_unchanged(self):
        """Rounds A-F were all measured without this section."""
        with_default = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
        )
        with_empty = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
            downstream=None,
        )

        assert with_default == with_empty
        assert "[CTE]" in with_default and "[CTE_GOAL]" in with_default


class TestSectionsAreExplained:
    """The payload labels its blocks `[USER_QUERY]`, `[CTE]`, `[MANDATORY_PROBES]` and so on,
    but the system prompt explains exactly one of them -- `[PREVIOUS_CTES]`. The model has
    to infer the rest from the label alone.

    That is why `[DOWNSTREAM]` alone did not prevent the contract break on local018: the
    section sat at 1.5% of a 15,791-character payload while the sentence explaining it sat
    at 99%, with 13,933 characters of injected rules in between.

    Gated on `downstream` being present, so the prompt stays byte-identical for rounds A-F.
    """

    def test_the_guide_appears_with_downstream(self):
        prompt = cte_refiner._system_prompt(predicted_ctes=None, downstream="SELECT * FROM a")

        for section in ("[USER_QUERY]", "[DOWNSTREAM]", "[CTE]", "[CTE_GOAL]",
                        "[MANDATORY_PROBES]"):
            assert section in prompt, section

    def test_downstream_is_explained_as_read_only_context(self):
        prompt = cte_refiner._system_prompt(predicted_ctes=None, downstream="SELECT * FROM a")

        assert "[DOWNSTREAM]" in prompt
        guide = prompt[prompt.index("[DOWNSTREAM]"):]
        assert "must not" in guide.lower() or "preserve" in guide.lower()

    def test_the_prompt_is_unchanged_without_downstream(self):
        """Rounds A-F were measured with the current prompt; drift here would move arm A
        for reasons unrelated to any experiment."""
        assert cte_refiner._system_prompt(predicted_ctes=None, downstream=None) == (
            cte_refiner.SYSTEM_PROMPT_BASE + "\n" + cte_refiner.SYSTEM_PROMPT_WITHOUT_PREDICTED
        )

    def test_the_predicted_variant_is_also_unchanged_without_downstream(self):
        assert cte_refiner._system_prompt(predicted_ctes="plan", downstream=None) == (
            cte_refiner.SYSTEM_PROMPT_BASE + "\n" + cte_refiner.SYSTEM_PROMPT_WITH_PREDICTED
        )

    def test_the_guide_is_added_to_whichever_variant_applies(self):
        with_plan = cte_refiner._system_prompt(predicted_ctes="plan", downstream="SELECT 1")

        assert cte_refiner.SYSTEM_PROMPT_WITH_PREDICTED.strip() in with_plan
        assert "[DOWNSTREAM]" in with_plan


class TestTheConstraintSitsWithTheSection:
    """162 characters of constraint at the tail of a 15,791-character payload is not where
    the model will connect it to a section near the top."""

    def test_the_constraint_follows_the_downstream_content(self):
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
            downstream="WITH b AS (SELECT x FROM a)\nSELECT * FROM b",
        )

        # The constraint sentence itself mentions `[CTE]`, so match the section header.
        assert payload.index("preserve the output column names") < payload.index("\n\n[CTE]\n")

    def test_it_is_not_at_the_tail_any_more(self):
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)", cte_goal="goal",
            downstream="SELECT * FROM a",
        )

        assert not payload.rstrip().endswith(cte_refiner.DOWNSTREAM_CONSTRAINT)

    def test_the_knowledge_block_no_longer_separates_them(self):
        """A realistic goal carries ~14k characters of rules; the constraint must not be on
        the far side of them."""
        payload = cte_refiner._build_user_payload(
            user_query="q", cte_text="WITH a AS (SELECT 1 AS x)",
            cte_goal="goal\n\nUse these tribal knowledge rules as guidance:\n" + ("- rule\n" * 2000),
            downstream="SELECT * FROM a",
        )

        gap = payload.index("[CTE_GOAL]") - payload.index("preserve the output column names")
        assert 0 < gap < 600, "the constraint should be a few hundred characters from its section"


class TestTheSelfCheckUsesTheValidator:
    @pytest.fixture
    def conn(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (x INTEGER)")
        yield c
        c.close()

    def test_a_validator_rejection_is_reported_as_a_needed_revision(self, conn):
        """Even though the fragment compiles on its own."""
        need, msgs = cte_refiner._check_suggested_sql(
            conn, conn.cursor(), "SELECT x FROM t",
            validate_fix_sql=lambda _sql: "no such column: speeding_incidents",
        )

        assert need is True
        assert any("speeding_incidents" in m for m in msgs)

    def test_the_message_says_it_failed_in_the_full_query(self, conn):
        """The refiner must not read this as its own fragment being broken."""
        _, msgs = cte_refiner._check_suggested_sql(
            conn, conn.cursor(), "SELECT x FROM t",
            validate_fix_sql=lambda _sql: "no such column: c",
        )

        joined = " ".join(msgs).lower()
        assert "full query" in joined
        assert "downstream" in joined

    def test_a_validator_pass_accepts_a_fragment_that_would_not_compile_alone(self, conn):
        """The fragment references a later CTE, so only the reassembled query can judge it."""
        need, msgs = cte_refiner._check_suggested_sql(
            conn, conn.cursor(), "SELECT x FROM a_cte_defined_elsewhere",
            validate_fix_sql=lambda _sql: None,
        )

        assert need is False
        assert msgs == []

    def test_the_validator_receives_the_suggested_sql(self, conn):
        seen = []
        cte_refiner._check_suggested_sql(
            conn, conn.cursor(), "SELECT x FROM t",
            validate_fix_sql=lambda sql: seen.append(sql) or None,
        )

        assert seen == ["SELECT x FROM t"]

    def test_without_a_validator_the_isolated_compile_still_decides(self, conn):
        """Rounds A-F must stay reproducible."""
        need, msgs = cte_refiner._check_suggested_sql(conn, conn.cursor(), "SELECT nope FROM t")

        assert need is True
        assert any("single executable" in m for m in msgs)

    def test_without_a_validator_a_compiling_fragment_passes(self, conn):
        need, msgs = cte_refiner._check_suggested_sql(conn, conn.cursor(), "SELECT x FROM t")

        assert (need, msgs) == (False, [])

    def test_an_empty_suggestion_is_not_checked(self, conn):
        need, msgs = cte_refiner._check_suggested_sql(
            conn, conn.cursor(), "", validate_fix_sql=lambda _s: pytest.fail("must not run"),
        )

        assert (need, msgs) == (False, [])


class TestRunnerWiring:
    def test_the_flag_defaults_to_off(self):
        assert _args(["--instance-id", "local001"]).validate_fix_in_context is False

    def test_it_is_carried_in_the_refiner_options(self):
        options = runner._refiner_options(
            _args(["--instance-id", "local001", "--adopt-refiner-sql", "--validate-fix-in-context"])
        )

        assert options["validate_fix_in_context"] is True

    def test_it_requires_adoption(self):
        """Without --adopt-refiner-sql the refiner's SQL is never substituted, so
        validating it against the full query measures nothing."""
        with pytest.raises(ValueError, match="validate-fix-in-context"):
            runner._refiner_options(_args(["--instance-id", "local001", "--validate-fix-in-context"]))

    def test_it_is_rejected_with_the_candidate_sql_flag(self):
        with pytest.raises(ValueError, match="validate-fix-in-context"):
            runner._refiner_options(
                _args(["--instance-id", "local001", "--include-candidate-sql",
                       "--validate-fix-in-context"])
            )


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
        if 'broken' in sql.lower():
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

    def test_the_first_cte_is_given_its_downstream(self, tmp_path, refiner_calls):
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True)

        first = refiner_calls[0]
        assert "shares" in first["downstream"]
        assert "SELECT * FROM shares" in first["downstream"]

    def test_the_last_cte_has_only_the_remainder_downstream(self, tmp_path, refiner_calls):
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True)

        second = refiner_calls[1]
        assert "SELECT * FROM shares" in second["downstream"]
        assert "totals AS (" not in second["downstream"], "upstream belongs to PREVIOUS_CTES"

    def test_a_validator_is_supplied(self, tmp_path, refiner_calls):
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True)

        assert callable(refiner_calls[0]["validate_fix_sql"])

    def test_the_validator_judges_the_reassembled_query(self, tmp_path, refiner_calls):
        """A fragment that only breaks once substituted must be rejected."""
        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True)
        validate = refiner_calls[0]["validate_fix_sql"]

        assert validate("WITH totals AS (\nSELECT 1 AS n FROM t\n)") is None
        assert "broken" in (validate("WITH totals AS (\nSELECT broken FROM t\n)") or "")

    def test_nothing_is_passed_when_the_flag_is_off(self, tmp_path, refiner_calls):
        """Rounds A-F must stay reproducible."""
        self._refine(tmp_path, adopt_refiner_sql=True)

        assert refiner_calls[0].get("downstream") in (None, "")
        assert refiner_calls[0].get("validate_fix_sql") is None

    def test_the_report_records_the_flag(self, tmp_path, monkeypatch, refiner_calls):
        monkeypatch.setattr(runner, "_retrieve_rules_for", lambda **_k: ([], []))

        self._refine(tmp_path, adopt_refiner_sql=True, validate_fix_in_context=True,
                     tkstore_path=str(tmp_path / "store.csv"))

        import json
        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        assert report["validate_fix_in_context"] is True
