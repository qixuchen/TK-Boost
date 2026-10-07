"""The reflector loop, driven by a scripted LLM and a fake probe executor."""

import json

import pytest

from tkstore.dataclaw.catalog import Catalog
from tkstore.dataclaw.devset import DevRun, LoadedRun
from tkstore.dataclaw.probe import ExecResult, ProbeSession
from tkstore.dataclaw.reflector import LLMReply, ReflectorConfig, build_messages, reflect, to_record
from tkstore.dataclaw.trajectory import Step, ToolCall, Trajectory

OPS = "enterprise/company_operation_status.csv"
CATALOG = Catalog(headers={OPS: ["bmCode", "secondTargetNum", "targetName", "value"]})

GOOD_FINAL = f"""<final>
DIVERGENCE: A1 kept only one targetName spelling
NEEDED: filter by secondTargetNum
MISSING_DATA_UNDERSTANDING:
  TABLES:
  COLUMNS: {OPS}.secondTargetNum, {OPS}.targetName
  FACT: one secondTargetNum has several targetName spellings
CATEGORY: 同一指标有多个名字
EVIDENCE: P1:L1
REPRODUCED: milestone "Number of companies" = 2778 FROM P1:L2
KIND: data
BASIS: data
INSTANCE: the agent kept only the spelling it saw first for the companies in the task
GENERALITY: P1:L2
ENSURE: select an indicator by secondTargetNum and keep every targetName under it
WHEN_TO_CHECK: the question uses a company-level indicator
TRIGGER: the highest revenue
CONTEXT: each company uses one targetName spelling per secondTargetNum
EXAMPLE_USAGE: Y_EC_5 is spelled 营收金额 for some companies
</final>"""

FORMAT_BAD_FINAL = GOOD_FINAL.replace("EVIDENCE: P1:L1", "EVIDENCE: P1:L9")
WRONG_VALUE_FINAL = GOOD_FINAL.replace('= 2778 FROM P1:L2', '= 1004 FROM P1:L1')
NO_DIVERGENCE_FINAL = "<final>\nNO_DATA_DIVERGENCE: cannot fix it without another probe\n</final>"
PROBE_TURN = "PLAN: count spellings\n<probe>\ncut -d, -f3,4 ./database/x.csv | sort | uniq -c\n</probe>"
PROBE_OUTPUT = "Y_EC_5 营收金额 1004\nY_EC_5 营业收入金额 2778"
JUDGE_ACCEPT = "VERDICT: accept\nREASON: holds for every indicator code"
JUDGE_REJECT = "VERDICT: reject\nREASON: only true for one named company"
REPRO_MATCH = (
    "REPRODUCED 1\nPROBE_QUANTITY: rows spelled 营业收入金额 under Y_EC_5\n"
    "MILESTONE_QUANTITY: companies reporting Y_EC_5\nVERDICT: match\nREASON: same quantity"
)
REPRO_MISMATCH = REPRO_MATCH.replace("VERDICT: match\nREASON: same quantity",
                                     "VERDICT: mismatch\nREASON: the probe counts rows, not companies")


class ScriptedLLM:
    def __init__(self, replies, default="PLAN: nothing"):
        self.replies = list(replies)
        self.default = default
        self.calls = []

    def __call__(self, messages):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Judges:
    """One judge LLM serving both judges; replies are routed by the judge's system prompt."""

    def __init__(self, generality=(), reproduction=()):
        self.generality = ScriptedLLM(generality, default=JUDGE_ACCEPT)
        self.reproduction = ScriptedLLM(reproduction, default=REPRO_MATCH)
        self.order = []

    def __call__(self, messages):
        stage = "reproduction" if "PROBE_QUANTITY" in messages[0]["content"] else "generality"
        self.order.append(stage)
        return getattr(self, stage)(messages)


class FakeExecutor:
    def __init__(self, outputs=None):
        self.commands = []
        self.outputs = dict(outputs or {})

    def start(self):
        pass

    def run(self, command, timeout):
        self.commands.append(command)
        return ExecResult(self.outputs.get(len(self.commands), PROBE_OUTPUT), 0, False)

    def stop(self):
        pass


@pytest.fixture
def loaded(tmp_path):
    run = DevRun(
        task_id="task_049_demo", run_dir=tmp_path, category="comprehensive_decision",
        level="medium", chat_sha256="", task_file=tmp_path / "t.md", gold_file=tmp_path / "g.json",
    )
    trajectory = Trajectory(
        prompt="Which province has the highest revenue?",
        steps=[Step(index=1, text="looking", calls=[ToolCall("a", "exec", "grep 营收金额 x.csv", "rows")])],
        final_answer="Guangdong",
    )
    return LoadedRun(
        run=run,
        prompt="Which province has the highest revenue?",
        gold={
            "answer": "Beijing",
            "steps": ["Filter Y_EC_5", "Sum by province"],
            "milestone": {"Number of companies": 2778, "Top province": "Beijing"},
        },
        score={"score": 0.0, "max_score": 1.0, "notes": "Agent answered Guangdong, gold is Beijing."},
        process_score={"gpr": {
            "break_point": 1,
            "chain_summary": "breaks at M1",
            "details": [
                {"key": "Number of companies", "expected": 2778, "achieved": False, "reason": "used 1004"},
                {"key": "Top province", "expected": "Beijing", "achieved": True, "reason": "ok"},
            ],
        }},
        trajectory=trajectory,
    )


def _reflect(loaded, replies, clock=None, judge=None, outputs=None, **config):
    llm = ScriptedLLM(replies)
    judge = judge if judge is not None else Judges()
    executor = FakeExecutor(outputs)
    with ProbeSession(executor, timeout=60, max_llm_chars=4000) as session:
        result = reflect(
            loaded, llm=llm, session=session, catalog=CATALOG, config=ReflectorConfig(**config),
            clock=clock, judge_llm=judge,
        )
    return result, llm, executor


def _clock(*values):
    ticks = iter(values)
    last = [values[-1]]

    def now():
        last[0] = next(ticks, last[0])
        return last[0]

    return now


EMPTY = LLMReply("", "length", False, 800.0)


def test_system_prompt_asks_for_every_field_the_gates_require(loaded):
    system = build_messages(loaded, CATALOG, ReflectorConfig())[0]["content"]
    for field in ["BASIS", "BASIS_QUOTE", "INSTANCE", "GENERALITY", "ENSURE", "WHEN_TO_CHECK",
                  "TRIGGER", "CONTEXT", "EXAMPLE_USAGE"]:
        assert f"{field}:" in system, field
    for basis in ["data", "task", "gold_only"]:
        assert f"- {basis}:" in system, basis


def test_system_prompt_describes_line_pointers_not_copied_excerpts(loaded):
    system = build_messages(loaded, CATALOG, ReflectorConfig())[0]["content"]
    assert "EVIDENCE: P<n>:L<a>" in system and "FROM P<n>:L<a>" in system
    assert "PROBE_RESULT P<n>" in system and "A<n>" in system and "CALL #" not in system
    assert "probe#" not in system and "SCOPE:" not in system
    assert "format retries" in system


def test_system_prompt_no_longer_bans_cell_values_in_the_rule_body(loaded):
    system = build_messages(loaded, CATALOG, ReflectorConfig())[0]["content"]
    assert "contain no\ncell value" not in system and "Any concrete cell value belongs in EXAMPLE_USAGE" not in system
    assert "may name the indicator" in system


def test_system_prompt_says_the_generality_reviewer_sees_task_gold_and_every_probe(loaded):
    system = build_messages(loaded, CATALOG, ReflectorConfig())[0]["content"]
    assert "(not the\ntask or the reference)" not in system
    assert "every probe you ran" in system


def test_system_prompt_describes_the_reproduction_review(loaded):
    system = build_messages(loaded, CATALOG, ReflectorConfig())[0]["content"]
    assert "It reads the code of your" in system
    assert "a number copied from the gold steps" in system
    assert "if no probe output shows the" in system


def test_reproduction_judge_is_told_it_has_the_file_list(loaded):
    judge = Judges()
    _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    assert "the files under database/ with their columns" in judge.reproduction.calls[0][0]["content"]


def test_messages_carry_every_input(loaded):
    messages = build_messages(loaded, CATALOG, ReflectorConfig())
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "<probe>" in messages[0]["content"] and "20" in messages[0]["content"]
    user = messages[1]["content"]
    for fragment in [
        "Which province has the highest revenue?",
        "AGENT CMD A1 exec: grep 营收金额 x.csv",
        "FINAL ANSWER: Guangdong",
        "Agent answered Guangdong, gold is Beijing.",
        "breaks at M1",
        "used 1004",
        "Filter Y_EC_5",
        f"{OPS}: bmCode, secondTargetNum, targetName, value",
    ]:
        assert fragment in user, fragment
    missed_section = user.split("Milestones you may reproduce", 1)[1]
    assert "Number of companies" in missed_section.split("\n\n", 1)[0]
    assert "Top province" not in missed_section.split("\n\n", 1)[0]


def test_probe_then_accepted_final(loaded):
    result, llm, executor = _reflect(loaded, [PROBE_TURN, GOOD_FINAL])
    assert executor.commands == ["cut -d, -f3,4 ./database/x.csv | sort | uniq -c"]
    assert "PROBE_RESULT P1 (exit 0; probes you have run: P1):\nL1| Y_EC_5 营收金额 1004" in llm.calls[1][-1]["content"]
    (accepted,) = result.accepted
    assert accepted.divergence.fact == "one secondTargetNum has several targetName spellings"
    assert accepted.divergence.evidence[0].excerpt == "Y_EC_5 营收金额 1004"
    assert accepted.divergence.scope == "multi_column"
    assert result.stop_reason == "done"
    assert result.finals == 1 and result.probes_used == 1 and result.format_retries == 0


def test_default_budget_is_five_finals_and_three_format_retries():
    config = ReflectorConfig()
    assert (config.max_finals, config.max_format_retries) == (5, 3)
    assert config.max_turns == config.max_probes + 5 + 3 + 3


def test_substantive_rejection_is_sent_back_and_uses_a_final(loaded):
    result, llm, _ = _reflect(loaded, [PROBE_TURN, WRONG_VALUE_FINAL, GOOD_FINAL])
    feedback = llm.calls[2][-1]["content"]
    assert "REJECTED" in feedback and "does not match gold" in feedback
    assert len(result.accepted) == 1 and result.finals == 2 and result.format_retries == 0
    assert result.rejected == []


def test_format_rejection_does_not_use_a_final(loaded):
    result, llm, _ = _reflect(loaded, [PROBE_TURN, FORMAT_BAD_FINAL, GOOD_FINAL])
    feedback = llm.calls[2][-1]["content"]
    assert "P1:L9 is outside the output of P1" in feedback
    assert len(result.accepted) == 1 and result.finals == 1 and result.format_retries == 1


def test_final_before_any_probe_is_a_format_error(loaded):
    result, llm, _ = _reflect(loaded, [GOOD_FINAL, PROBE_TURN, GOOD_FINAL])
    assert "no probe has been run yet" in llm.calls[1][-1]["content"]
    assert len(result.accepted) == 1 and result.finals == 1 and result.format_retries == 1


def test_mixed_final_uses_a_final_and_skips_the_malformed_divergence(loaded):
    body = lambda final: final.removeprefix("<final>\n").removesuffix("</final>")
    mixed = "<final>\n" + body(FORMAT_BAD_FINAL) + body(WRONG_VALUE_FINAL) + "</final>"
    judge = Judges()
    result, llm, _ = _reflect(loaded, [PROBE_TURN, mixed, GOOD_FINAL], judge=judge)
    feedback = llm.calls[2][-1]["content"]
    assert "outside the output" in feedback and "does not match gold" in feedback
    assert result.finals == 2 and result.format_retries == 0
    assert len(judge.generality.calls) == 2


def test_verdict_tells_how_many_probes_and_finals_are_left(loaded):
    _, llm, _ = _reflect(loaded, [PROBE_TURN, WRONG_VALUE_FINAL, GOOD_FINAL])
    feedback = llm.calls[2][-1]["content"]
    assert "19 probe(s) left" in feedback and "4 <final> submission(s) left" in feedback
    assert "run more probes" in feedback


def test_final_budget_exhausted(loaded):
    replies = [PROBE_TURN, WRONG_VALUE_FINAL, WRONG_VALUE_FINAL, WRONG_VALUE_FINAL]
    result, _, _ = _reflect(loaded, replies, max_finals=3)
    assert result.stop_reason == "final_budget"
    assert result.accepted == [] and len(result.rejected) == 1


def test_format_budget_exhausted(loaded):
    replies = [PROBE_TURN, FORMAT_BAD_FINAL, FORMAT_BAD_FINAL, FORMAT_BAD_FINAL]
    result, _, _ = _reflect(loaded, replies, max_format_retries=3)
    assert result.stop_reason == "format_budget"
    assert result.finals == 0 and result.format_retries == 3 and len(result.rejected) == 1


def test_giving_up_after_a_rejection_is_recorded(loaded):
    result, _, _ = _reflect(loaded, [PROBE_TURN, WRONG_VALUE_FINAL, NO_DIVERGENCE_FINAL])
    assert result.stop_reason == "abandoned_after_reject"
    assert result.accepted == []


def test_no_divergence_without_a_rejection_is_done(loaded):
    result, _, _ = _reflect(loaded, [PROBE_TURN, NO_DIVERGENCE_FINAL])
    assert result.stop_reason == "done"


def test_probe_budget_blocks_further_probes(loaded):
    result, llm, executor = _reflect(loaded, [PROBE_TURN, PROBE_TURN, GOOD_FINAL], max_probes=1)
    assert len(executor.commands) == 1
    assert "probe budget" in llm.calls[2][-1]["content"]
    assert len(result.accepted) == 1


def test_invalid_turn_gets_the_error_back(loaded):
    result, llm, _ = _reflect(loaded, ["just thinking", PROBE_TURN, GOOD_FINAL])
    assert "neither a <probe> nor a <final>" in llm.calls[1][-1]["content"]
    assert len(result.accepted) == 1


def test_turn_cap_stops_a_stuck_reflector(loaded):
    result, llm, _ = _reflect(loaded, [], max_probes=2, max_finals=1)
    assert result.stop_reason == "turn_budget"
    assert len(llm.calls) == ReflectorConfig(max_probes=2, max_finals=1).max_turns


def test_non_data_divergence_is_logged(loaded):
    final = "<final>\nDIVERGENCE: subtracted the wrong way\nNEEDED: flip it\nKIND: non_data\n</final>"
    result, _, _ = _reflect(loaded, [final])
    assert result.accepted == [] and len(result.logged) == 1
    assert result.stop_reason == "done"


def test_llm_replies_are_used_and_their_stats_recorded(loaded):
    replies = [LLMReply(PROBE_TURN, "stop", False, 12.5), LLMReply(GOOD_FINAL, "stop", False, 30.0)]
    judge = Judges(generality=[LLMReply(JUDGE_ACCEPT, "stop", False, 4.0), LLMReply(JUDGE_ACCEPT, "stop", False, 5.0)],
                   reproduction=[LLMReply(REPRO_MATCH, "stop", False, 3.0)])
    result, _, _ = _reflect(loaded, replies, judge=judge)
    assert len(result.accepted) == 1
    assert [(c["stage"], c["finish_reason"], c["elapsed_s"], c["timed_out"]) for c in result.llm_calls] == [
        ("reflect", "stop", 12.5, False),
        ("reflect", "stop", 30.0, False),
        ("judge_reproduction", "stop", 3.0, False),
        ("judge_generality", "stop", 4.0, False),
        ("judge_generality", "stop", 5.0, False),
    ]
    assert result.llm_calls[0]["text_chars"] == len(PROBE_TURN)


def test_judge_replies_are_saved_with_their_divergence(loaded):
    judge = Judges(generality=[JUDGE_ACCEPT, "VERDICT: accept\nREASON: second vote"])
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    judged = [c for c in result.llm_calls if c["stage"] != "reflect"]
    assert [(c["stage"], c["divergence"], c["text"]) for c in judged] == [
        ("judge_reproduction", 1, REPRO_MATCH),
        ("judge_generality", 1, JUDGE_ACCEPT),
        ("judge_generality", 1, "VERDICT: accept\nREASON: second vote"),
    ]
    assert all("text" not in c for c in result.llm_calls if c["stage"] == "reflect")


def test_generality_judge_takes_the_majority_of_three_votes(loaded):
    judge = Judges(generality=[JUDGE_ACCEPT, JUDGE_REJECT, JUDGE_ACCEPT])
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    assert len(judge.generality.calls) == 3 and len(result.accepted) == 1


def test_generality_majority_reject_sends_back_every_rejecting_reason(loaded):
    other = "VERDICT: reject\nREASON: no probe covers other indicators"
    judge = Judges(generality=[JUDGE_REJECT, JUDGE_ACCEPT, other])
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge, max_finals=1)
    assert len(judge.generality.calls) == 3 and result.accepted == []
    assert result.rejected[0].reasons == [
        "GENERALITY JUDGE: only true for one named company",
        "GENERALITY JUDGE: no probe covers other indicators",
    ]


def test_two_agreeing_votes_skip_the_third(loaded):
    judge = Judges(generality=[JUDGE_REJECT, JUDGE_REJECT, JUDGE_ACCEPT])
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge, max_finals=1)
    assert len(judge.generality.calls) == 2 and result.accepted == []


def test_judge_sees_the_divergence_rule_and_generality_probe(loaded):
    judge = Judges()
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    assert len(result.accepted) == 1
    messages = judge.generality.calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "VERDICT" in messages[0]["content"]
    user = messages[1]["content"]
    for fragment in (
        "FACT: one secondTargetNum has several targetName spellings",
        "BASIS: data",
        "INSTANCE: the agent kept only the spelling",
        "ENSURE: select an indicator by secondTargetNum",
        "EXAMPLE_USAGE: Y_EC_5 is spelled 营收金额",
        "cut -d, -f3,4 ./database/x.csv | sort | uniq -c",
        "L2| Y_EC_5 营业收入金额 2778",
    ):
        assert fragment in user, fragment


def test_judge_sees_the_task_the_gold_and_every_probe(loaded):
    judge = Judges()
    other_probe = "PLAN: look at units\n<probe>\nhead -3 ./database/units.csv\n</probe>"
    _reflect(loaded, [PROBE_TURN, other_probe, GOOD_FINAL], judge=judge, outputs={2: "targetUnit\n元\n万元"})
    user = judge.generality.calls[0][1]["content"]
    for fragment in (
        "Which province has the highest revenue?",
        '"Beijing"',
        "1. Filter Y_EC_5",
        '"Number of companies": 2778',
        "P2: head -3 ./database/units.csv",
        "L3| 万元",
    ):
        assert fragment in user, fragment


def test_judge_sees_long_probe_output_cut_by_whole_lines(loaded):
    judge = Judges()
    long_output = PROBE_OUTPUT + "\n" + "\n".join(f"row {i} " + "x" * 40 for i in range(3, 1001))
    _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge, outputs={1: long_output})
    user = judge.generality.calls[0][1]["content"]
    assert "L3| row 3 " in user and "L1000| row 1000 " in user
    assert "omitted]" in user
    assert len(user) < len(long_output)


def test_judge_prompt_tests_value_replacement_and_gold_steps_regardless_of_basis():
    from tkstore.dataclaw.reflector import JUDGE_PROMPT_PATH

    prompt = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    assert "Replace each value" in prompt
    assert "Whatever BASIS says" in prompt
    assert "gold steps" in prompt


def test_judge_prompt_defines_basis_and_spares_rules_the_task_wording_requires(loaded):
    judge = Judges()
    _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    system = judge.generality.calls[0][0]["content"]
    for definition in ("- data: the data forces it", "- task: the wording of the question decides it",
                       "- gold_only: neither the data nor the question forces it"):
        assert definition in system, definition
    assert "the task wording quoted on BASIS_QUOTE" in system


GENERALITY_JUDGE_FIELDS = (
    "DIVERGENCE", "NEEDED", "BASIS", "BASIS_QUOTE", "INSTANCE", "SCOPE", "TABLES", "COLUMNS", "FACT",
    "CATEGORY", "EVIDENCE", "GENERALITY", "ENSURE", "WHEN_TO_CHECK", "TRIGGER", "CONTEXT", "EXAMPLE_USAGE",
)


def test_generality_judge_is_told_what_every_field_it_sees_means(loaded):
    judge = Judges()
    task_final = GOOD_FINAL.replace("BASIS: data", "BASIS: task\nBASIS_QUOTE: the highest revenue")
    _reflect(loaded, [PROBE_TURN, task_final], judge=judge)
    system, user = (m["content"] for m in judge.generality.calls[0])
    for name in GENERALITY_JUDGE_FIELDS:
        assert f"\n{name}: " in "\n" + user, name
        assert f"\n- {name}: " in system, name


def test_both_judges_see_the_files_and_their_columns(loaded):
    judge = Judges()
    _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    for calls in (judge.reproduction.calls, judge.generality.calls):
        user = calls[0][1]["content"]
        assert "## Files under ./database/ and their columns" in user
        assert f"{OPS}: bmCode, secondTargetNum, targetName, value" in user


def test_generality_judge_checks_wording_then_evidence_from_probe_outputs_only(loaded):
    judge = Judges()
    _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    system = judge.generality.calls[0][0]["content"]
    assert "a. Wording." in system and "b. Evidence." in system
    assert "the file list in the input" in system and "file list below" not in system
    assert "Judge this only from the probe outputs" in system
    assert "say what probe would show it" in system


def test_reproduction_judge_needed_meaning_does_not_mention_basis(loaded):
    judge = Judges()
    _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    system = judge.reproduction.calls[0][0]["content"]
    needed = system.split("\n- NEEDED: ", 1)[1].split("\n- ", 1)[0]
    assert "BASIS" not in needed


def test_reproduction_judge_is_told_what_its_fields_mean_and_only_those(loaded):
    judge = Judges()
    _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    system = judge.reproduction.calls[0][0]["content"]
    for name in ("DIVERGENCE", "NEEDED", "INSTANCE", "REPRODUCED", "SEMANTIC_MATCH"):
        assert f"\n- {name}: " in system, name
    for name in ("ENSURE", "WHEN_TO_CHECK", "GENERALITY", "BASIS"):
        assert f"\n- {name}: " not in system, name


def test_judge_rejection_is_sent_back_and_fix_is_accepted(loaded):
    judge = Judges(generality=[JUDGE_REJECT, JUDGE_REJECT, JUDGE_ACCEPT, JUDGE_ACCEPT])
    result, llm, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL, GOOD_FINAL], judge=judge)
    feedback = llm.calls[2][-1]["content"]
    assert "REJECTED" in feedback and "GENERALITY JUDGE: only true for one named company" in feedback
    assert len(result.accepted) == 1 and result.finals == 2 and result.rejected == []


def test_judge_rejection_uses_up_the_final_budget(loaded):
    judge = Judges(generality=[JUDGE_REJECT, JUDGE_REJECT])
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge, max_finals=1)
    assert result.stop_reason == "final_budget"
    assert result.accepted == []
    assert result.rejected[0].reasons == ["GENERALITY JUDGE: only true for one named company"] * 2


def test_unparseable_judge_reply_is_retried_once(loaded):
    judge = Judges(generality=["", JUDGE_ACCEPT])
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    assert len(result.accepted) == 1 and len(judge.generality.calls) == 3


def test_judge_failing_twice_is_a_judge_error_not_sent_back(loaded):
    judge = Judges(generality=["", "I think it is fine"])
    result, llm, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    assert result.accepted == [] and result.rejected == []
    assert len(result.judge_errors) == 1
    assert result.judge_errors[0].divergence.fact == "one secondTargetNum has several targetName spellings"
    assert result.stop_reason == "done" and len(llm.calls) == 2


def test_divergence_failing_the_gates_is_not_judged(loaded):
    judge = Judges()
    result, _, _ = _reflect(loaded, [PROBE_TURN, WRONG_VALUE_FINAL, GOOD_FINAL], judge=judge)
    assert judge.order == ["reproduction", "generality", "generality"] and len(result.accepted) == 1


def test_reproduction_judge_runs_before_the_generality_judge(loaded):
    judge = Judges()
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    assert judge.order == ["reproduction", "generality", "generality"] and len(result.accepted) == 1


def test_reproduction_judge_sees_the_claim_the_code_and_the_gold(loaded):
    judge = Judges()
    other_probe = "PLAN: look at units\n<probe>\nhead -3 ./database/units.csv\n</probe>"
    _reflect(loaded, [PROBE_TURN, other_probe, GOOD_FINAL], judge=judge, outputs={2: "targetUnit\n元\n万元"})
    (messages,) = judge.reproduction.calls
    user = messages[1]["content"]
    for fragment in (
        "Which province has the highest revenue?",
        '"Beijing"',
        "1. Filter Y_EC_5",
        '"Number of companies": 2778',
        "Number of companies (expected 2778): MISSED - used 1004",
        'REPRODUCED 1: milestone "Number of companies" (gold 2778) = 2778 FROM P1:L2 → Y_EC_5 营业收入金额 2778',
        "DIVERGENCE: A1 kept only one targetName spelling",
        "NEEDED: filter by secondTargetNum",
        "INSTANCE: the agent kept only the spelling",
        "P1: cut -d, -f3,4 ./database/x.csv | sort | uniq -c",
        "P2: head -3 ./database/units.csv",
    ):
        assert fragment in user, fragment
    assert "grep 营收金额 x.csv" not in user
    assert "ENSURE:" not in user


def test_reproduction_judge_sees_the_semantic_match_note(loaded):
    final = GOOD_FINAL.replace("FROM P1:L2\n", "FROM P1:L2\nSEMANTIC_MATCH: the count of 营业收入金额 rows\n")
    judge = Judges()
    _reflect(loaded, [PROBE_TURN, final], judge=judge)
    assert "SEMANTIC_MATCH: the count of 营业收入金额 rows" in judge.reproduction.calls[0][1]["content"]


def test_reproduction_mismatch_is_sent_back_and_uses_a_final(loaded):
    judge = Judges(reproduction=[REPRO_MISMATCH, REPRO_MATCH])
    result, llm, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL, GOOD_FINAL], judge=judge)
    feedback = llm.calls[2][-1]["content"]
    assert "REJECTED" in feedback and "REPRODUCTION JUDGE: the probe counts rows, not companies" in feedback
    assert judge.order == ["reproduction", "reproduction", "generality", "generality"]
    assert len(result.accepted) == 1 and result.finals == 2 and result.format_retries == 0


def test_unparseable_reproduction_reply_is_retried_then_a_judge_error(loaded):
    judge = Judges(reproduction=["VERDICT: accept", "no verdict here"])
    result, llm, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL], judge=judge)
    assert len(judge.reproduction.calls) == 2 and judge.generality.calls == []
    assert result.accepted == [] and result.rejected == [] and len(result.judge_errors) == 1
    assert "reproduction judge" in result.judge_errors[0].reasons[0]
    assert result.stop_reason == "done" and len(llm.calls) == 2


def test_reproduction_reply_gives_one_verdict_per_reproduced_line():
    from tkstore.dataclaw.reflector import parse_reproduction_judge

    reply = REPRO_MATCH + "\n\n" + REPRO_MISMATCH.replace("REPRODUCED 1", "REPRODUCED 2")
    first, second = parse_reproduction_judge(reply, 2)
    assert (first.verdict, first.probe_quantity) == ("match", "rows spelled 营业收入金额 under Y_EC_5")
    assert (second.verdict, second.reason) == ("mismatch", "the probe counts rows, not companies")
    assert second.milestone_quantity == "companies reporting Y_EC_5"


def test_reproduction_reply_missing_a_verdict_is_unparseable():
    from tkstore.dataclaw.reflector import parse_reproduction_judge

    assert parse_reproduction_judge(REPRO_MATCH, 2) is None
    assert parse_reproduction_judge(REPRO_MATCH.replace("VERDICT: match", "VERDICT: maybe"), 1) is None


def test_judge_defaults_to_the_reflector_llm(loaded):
    llm = ScriptedLLM([PROBE_TURN, GOOD_FINAL, REPRO_MATCH, JUDGE_ACCEPT, JUDGE_ACCEPT])
    with ProbeSession(FakeExecutor(), timeout=60, max_llm_chars=4000) as session:
        result = reflect(loaded, llm=llm, session=session, catalog=CATALOG, config=ReflectorConfig())
    assert len(result.accepted) == 1
    assert "PROBE_QUANTITY" in llm.calls[2][0]["content"]
    assert "Replace each value" in llm.calls[3][0]["content"]


def test_basis_quote_is_checked_against_the_task(loaded):
    task_final = GOOD_FINAL.replace("BASIS: data", "BASIS: task\nBASIS_QUOTE: the lowest revenue")
    result, llm, _ = _reflect(loaded, [PROBE_TURN, task_final, GOOD_FINAL])
    assert "BASIS_QUOTE 'the lowest revenue' does not appear in the task" in llm.calls[2][-1]["content"]
    assert len(result.accepted) == 1


def test_one_empty_reply_gets_a_specific_nudge_and_the_run_recovers(loaded):
    result, llm, _ = _reflect(loaded, [EMPTY, PROBE_TURN, GOOD_FINAL])
    nudge = llm.calls[1][-1]["content"]
    assert "cut off before any text" in nudge and "FORMAT ERROR" not in nudge
    assert len(result.accepted) == 1 and result.stop_reason == "done"


def test_two_consecutive_empty_replies_end_the_run(loaded):
    result, llm, _ = _reflect(loaded, [PROBE_TURN, EMPTY, EMPTY, GOOD_FINAL])
    assert result.stop_reason == "empty_reply"
    assert len(llm.calls) == 3
    assert result.probes_used == 1


def test_empty_replies_separated_by_a_real_one_do_not_end_the_run(loaded):
    result, _, _ = _reflect(loaded, [EMPTY, PROBE_TURN, EMPTY, GOOD_FINAL])
    assert result.stop_reason == "done" and len(result.accepted) == 1


def test_timed_out_empty_reply_counts_as_empty(loaded):
    timed_out = LLMReply("", "timeout", True, 300.0)
    result, _, _ = _reflect(loaded, [timed_out, timed_out])
    assert result.stop_reason == "empty_reply"


def test_run_over_time_budget_is_told_to_finish(loaded):
    clock = _clock(0, 50, 150)
    result, llm, _ = _reflect(loaded, [PROBE_TURN, PROBE_TURN, GOOD_FINAL], clock=clock, run_time_budget_s=100)
    assert "time budget" in llm.calls[2][-1]["content"].lower()
    assert "time budget" not in llm.calls[1][-1]["content"].lower()
    assert result.stop_reason == "done" and len(result.accepted) == 1


def test_run_still_over_budget_after_the_warning_stops(loaded):
    clock = _clock(0, 150, 200)
    result, llm, _ = _reflect(loaded, [PROBE_TURN, PROBE_TURN, GOOD_FINAL], clock=clock, run_time_budget_s=100)
    assert result.stop_reason == "time_budget"
    assert len(llm.calls) == 2


def test_llm_error_keeps_the_partial_run(loaded):
    result, _, _ = _reflect(loaded, [PROBE_TURN, RuntimeError("gateway down")])
    assert result.stop_reason == "error"
    assert "gateway down" in result.error
    assert result.probes_used == 1 and len(result.probes) == 1
    assert any("PROBE_RESULT P1" in m["content"] for m in result.messages)


def test_interrupt_keeps_the_partial_run(loaded):
    result, _, _ = _reflect(loaded, [PROBE_TURN, KeyboardInterrupt()])
    assert result.stop_reason == "interrupted"
    assert len(result.probes) == 1


def test_record_carries_error_and_call_stats(loaded):
    result, _, _ = _reflect(loaded, [LLMReply(PROBE_TURN, "stop", False, 1.0), RuntimeError("boom")])
    record = json.loads(json.dumps(to_record(loaded, result), ensure_ascii=False))
    assert record["stop_reason"] == "error" and "boom" in record["error"]
    assert record["llm_calls"][0]["finish_reason"] == "stop"


def test_record_is_json_serializable(loaded):
    result, _, _ = _reflect(loaded, [PROBE_TURN, GOOD_FINAL])
    record = json.loads(json.dumps(to_record(loaded, result), ensure_ascii=False))
    assert record["task_id"] == "task_049_demo"
    assert record["accepted"][0]["divergence"]["columns"][0] == [OPS, "secondTargetNum"]
    assert record["probes"][0]["output"] == PROBE_OUTPUT
    assert record["stop_reason"] == "done"
    assert record["judge_errors"] == []
    assert record["format_retries"] == 0
    generality = record["accepted"][0]["divergence"]["generality"][0]
    assert (generality["probe"], generality["start"], generality["excerpt"]) == (1, 2, "Y_EC_5 营业收入金额 2778")
