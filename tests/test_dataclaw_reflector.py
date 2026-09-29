"""The reflector loop, driven by a scripted LLM and a fake probe executor."""

import json

import pytest

from tkstore.dataclaw.catalog import Catalog
from tkstore.dataclaw.devset import DevRun, LoadedRun
from tkstore.dataclaw.probe import ExecResult, ProbeSession
from tkstore.dataclaw.reflector import LLMReply, ReflectorConfig, build_messages, reflect, to_record
from tkstore.dataclaw.trajectory import Step, ToolCall, Trajectory

OPS = "enterprise/company_operation_status.csv"
CATALOG = Catalog(headers={OPS: ["bmCode", "secondTargetNum", "targetName", "value"]}, values=frozenset())

GOOD_FINAL = f"""<final>
DIVERGENCE: CALL #1 kept only one targetName spelling
NEEDED: filter by secondTargetNum
MISSING_DATA_UNDERSTANDING:
  SCOPE: multi_column
  TABLES:
  COLUMNS: {OPS}.secondTargetNum, {OPS}.targetName
  FACT: one secondTargetNum has several targetName spellings
CATEGORY: 同一指标有多个名字
EVIDENCE: probe#1 → Y_EC_5 营收金额 1004
REPRODUCED: milestone "Number of companies" = 2778 FROM probe#1
KIND: data
</final>"""

BAD_FINAL = GOOD_FINAL.replace("Y_EC_5 营收金额 1004", "Y_EC_5 营收金额 9999")
PROBE_TURN = "PLAN: count spellings\n<probe>\ncut -d, -f3,4 ./database/x.csv | sort | uniq -c\n</probe>"
PROBE_OUTPUT = "Y_EC_5 营收金额 1004\nY_EC_5 营业收入金额 2778"


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0) if self.replies else "PLAN: nothing"
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeExecutor:
    def __init__(self):
        self.commands = []

    def start(self):
        pass

    def run(self, command, timeout):
        self.commands.append(command)
        return ExecResult(PROBE_OUTPUT, 0, False)

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


def _reflect(loaded, replies, clock=None, **config):
    llm = ScriptedLLM(replies)
    executor = FakeExecutor()
    with ProbeSession(executor, timeout=60, max_llm_chars=4000) as session:
        result = reflect(
            loaded, llm=llm, session=session, catalog=CATALOG, config=ReflectorConfig(**config), clock=clock
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


def test_messages_carry_every_input(loaded):
    messages = build_messages(loaded, CATALOG, ReflectorConfig())
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "<probe>" in messages[0]["content"] and "20" in messages[0]["content"]
    user = messages[1]["content"]
    for fragment in [
        "Which province has the highest revenue?",
        "CALL #1 exec: grep 营收金额 x.csv",
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
    assert "PROBE_RESULT #1 (exit 0)" in llm.calls[1][-1]["content"]
    assert [g.divergence.fact for g in result.accepted] == ["one secondTargetNum has several targetName spellings"]
    assert result.stop_reason == "done"
    assert result.finals == 1 and result.probes_used == 1


def test_rejection_is_sent_back_and_fix_is_accepted(loaded):
    result, llm, _ = _reflect(loaded, [PROBE_TURN, BAD_FINAL, GOOD_FINAL])
    feedback = llm.calls[2][-1]["content"]
    assert "REJECTED" in feedback and "not found in the output of probe#1" in feedback
    assert len(result.accepted) == 1 and result.finals == 2
    assert result.rejected == []


def test_final_budget_exhausted(loaded):
    result, _, _ = _reflect(loaded, [PROBE_TURN, BAD_FINAL, BAD_FINAL, BAD_FINAL], max_finals=3)
    assert result.stop_reason == "final_budget"
    assert result.accepted == [] and len(result.rejected) == 1


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
    result, _, _ = _reflect(loaded, replies)
    assert len(result.accepted) == 1
    assert [(c["finish_reason"], c["elapsed_s"], c["timed_out"]) for c in result.llm_calls] == [
        ("stop", 12.5, False),
        ("stop", 30.0, False),
    ]
    assert result.llm_calls[0]["text_chars"] == len(PROBE_TURN)


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
    assert any("PROBE_RESULT #1" in m["content"] for m in result.messages)


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
