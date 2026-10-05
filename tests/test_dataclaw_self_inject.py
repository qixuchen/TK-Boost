"""Self-injection test (TK-Boost-adapt.md 9.6): rule files, agent-only injection, delivery checks."""

import json
from types import SimpleNamespace

import pytest

from tkstore.dataclaw.self_inject import (
    DeliveryError,
    agent_prompt,
    build_rules,
    delivered,
    missing_rules,
    render_notes,
    rule_sets,
    run_injected,
    summarize,
)


def _divergence(index, when, ensure, context, basis="data"):
    return {
        "index": index,
        "when_to_check": when,
        "ensure": ensure,
        "context": context,
        "basis": basis,
        "example_usage": "EXAMPLE TEXT (the gold here)",
        "instance": "INSTANCE TEXT",
        "fact": "FACT TEXT",
        "trigger": "TRIGGER TEXT",
    }


def _record(task_id, run_dir, divergences):
    return {"task_id": task_id, "run_dir": run_dir, "model": "openai/gpt-5.1",
            "accepted": [{"divergence": d} for d in divergences]}


def test_notes_carry_the_three_fields_verbatim_in_order():
    notes = render_notes([_divergence(1, "W one", "E one", "C one")])

    assert notes.startswith("[DATABASE NOTES]\n")
    assert "Note 1\nApplies when: W one\nCheck: E one\nWhy: C one\n[END DATABASE NOTES]" in notes
    assert notes.rstrip().endswith("The answer must still follow the output guidelines in the question above.")


def test_notes_leave_out_every_other_field():
    notes = render_notes([_divergence(1, "W", "E", "C")])

    for text in ("EXAMPLE TEXT", "INSTANCE TEXT", "FACT TEXT", "TRIGGER TEXT"):
        assert text not in notes


def test_several_rules_are_numbered_in_order():
    notes = render_notes([_divergence(1, "W1", "E1", "C1"), _divergence(2, "W2", "E2", "C2")])

    assert notes.index("Note 1\nApplies when: W1") < notes.index("Note 2\nApplies when: W2")
    assert notes.count("[DATABASE NOTES]") == 1


def test_rule_sets_group_exported_rules_by_task():
    exported = [
        {"divergence_id": "task_009__aaaaaa__1", "task_id": "task_009", "divergence": _divergence(1, "W1", "E1", "C1")},
        {"divergence_id": "task_011__bbbbbb__1", "task_id": "task_011",
         "divergence": _divergence(1, "W2", "E2", "C2", basis="gold_only")},
        {"divergence_id": "task_009__aaaaaa__2", "task_id": "task_009", "divergence": _divergence(2, "W3", "E3", "C3")},
    ]

    sets = rule_sets(exported)

    assert list(sets) == ["task_009", "task_011"]
    assert sets["task_009"].ids == ["task_009__aaaaaa__1", "task_009__aaaaaa__2"]
    assert sets["task_009"].basis == ["data", "data"]
    assert "Note 2\nApplies when: W3" in sets["task_009"].text
    assert sets["task_011"].basis == ["gold_only"]


def test_build_rules_reads_only_the_given_round_and_clears_old_files(tmp_path):
    round_dir = tmp_path / "rev2"
    (round_dir / "older").mkdir(parents=True)
    (round_dir / "task_009__run.json").write_text(
        json.dumps(_record("task_009", "glm-5.2_x_aaaaaa", [_divergence(1, "W", "E", "C")])), encoding="utf-8")
    (round_dir / "task_352__run.json").write_text(json.dumps(_record("task_352", "glm-5.2_y_cccccc", [])),
                                                   encoding="utf-8")
    (round_dir / "older" / "task_218__run.json").write_text(
        json.dumps(_record("task_218", "glm-5.2_z_bbbbbb", [_divergence(1, "W", "E", "C")])), encoding="utf-8")
    out = tmp_path / "rules"
    out.mkdir()
    (out / "task_231.md").write_text("stale", encoding="utf-8")

    build_rules(round_dir, out)

    assert sorted(p.name for p in out.iterdir()) == ["index.json", "task_009.md"]
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert index["source"] == str(round_dir)
    assert index["tasks"]["task_009"] == {"file": "task_009.md", "ids": ["task_009__aaaaaa__1"], "basis": ["data"]}
    assert (out / "task_009.md").read_text(encoding="utf-8") == render_notes([_divergence(1, "W", "E", "C")])


def test_agent_prompt_keeps_the_task_first_and_appends_the_notes():
    assert agent_prompt("Question?\n\nOutput guidelines: a number", "[DATABASE NOTES]\n...\n") == (
        "Question?\n\nOutput guidelines: a number\n\n[DATABASE NOTES]\n...\n"
    )


def _chat(path, user_text):
    lines = [
        {"type": "session", "id": "chat"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": user_text}]}},
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "42"}]}},
    ]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")


def _rules_dir(tmp_path, task_id="task_009"):
    rules = tmp_path / "rules"
    reflect = tmp_path / "reflect"
    reflect.mkdir()
    (reflect / f"{task_id}__run.json").write_text(
        json.dumps(_record(task_id, "glm-5.2_x_aaaaaa", [_divergence(1, "W", "E", "C")])), encoding="utf-8")
    build_rules(reflect, rules)
    return rules


class _FakeRunBatch:
    """Stands in for dataclaw.eval.run_batch: writes the agent prompt into chat.jsonl and calls grade_task."""

    def __init__(self, out_root, deliver=True, fail=False):
        self.out_root = out_root
        self.deliver = deliver
        self.fail = fail
        self.agent_prompts = []
        self.judge_prompts = []
        self.grade_task = self._real_grade

    def _real_grade(self, **kwargs):
        self.judge_prompts.append(kwargs["task_prompt"])

    def run_single_task(self, task, model, judge_model, timeout_multiplier, skill_path=None):
        self.agent_prompts.append(task.prompt)
        if self.fail:
            raise RuntimeError("docker died")
        out = self.out_root / task.task_id / "gpt-5.1_x_abcdef"
        out.mkdir(parents=True)
        sent = task.prompt.rstrip("\n") if self.deliver else "something else"
        _chat(out / "chat.jsonl", "[Mon 2026-10-05 02:00 UTC] " + sent)
        self.grade_task(task_id=task.task_id, task_prompt=task.prompt, agent_transcript_path=out / "chat.jsonl")
        return {"task_id": task.task_id, "output_dir": str(out), "error": None}


def _task(task_id="task_009"):
    return SimpleNamespace(task_id=task_id, prompt="Question?\n\nOutput guidelines: a number")


def _run(fake, rules, tmp_path, task=None):
    return run_injected(fake, task or _task(), rules, model="gpt-5.1", judge_model="judge",
                        timeout_multiplier=1.0, failures_path=tmp_path / "out" / "self_inject_failures.jsonl")


def test_agent_gets_the_notes_and_the_judge_gets_the_original_prompt(tmp_path):
    rules = _rules_dir(tmp_path)
    fake = _FakeRunBatch(tmp_path / "out")
    task = _task()

    _run(fake, rules, tmp_path, task)

    notes = (rules / "task_009.md").read_text(encoding="utf-8")
    assert fake.agent_prompts == [agent_prompt("Question?\n\nOutput guidelines: a number", notes)]
    assert fake.judge_prompts == ["Question?\n\nOutput guidelines: a number"]
    assert task.prompt == "Question?\n\nOutput guidelines: a number"
    assert fake.grade_task == fake._real_grade


def test_grade_task_is_restored_when_the_run_raises(tmp_path):
    rules = _rules_dir(tmp_path)
    fake = _FakeRunBatch(tmp_path / "out", fail=True)

    with pytest.raises(RuntimeError, match="docker died"):
        _run(fake, rules, tmp_path)

    assert fake.grade_task == fake._real_grade


def test_receipt_records_what_was_injected(tmp_path):
    import hashlib

    rules = _rules_dir(tmp_path)
    result = _run(_FakeRunBatch(tmp_path / "out"), rules, tmp_path)

    receipt = json.loads((tmp_path / "out/task_009/gpt-5.1_x_abcdef/knowledge_receipt.json").read_text("utf-8"))
    notes = (rules / "task_009.md").read_text(encoding="utf-8")
    assert receipt["rule_file"] == str(rules / "task_009.md")
    assert receipt["rule_file_sha256"] == hashlib.sha256(notes.encode("utf-8")).hexdigest()
    assert receipt["rule_ids"] == ["task_009__aaaaaa__1"]
    assert receipt["basis"] == ["data"]
    assert receipt["injected_text"] == notes
    assert receipt["injected_sha256"] == hashlib.sha256(notes.encode("utf-8")).hexdigest()
    assert receipt["injected_chars"] == len(notes)
    assert receipt["delivery_ok"] is True
    assert result["knowledge_receipt"] == receipt


def test_delivery_accepts_a_timestamped_first_user_message(tmp_path):
    chat = tmp_path / "chat.jsonl"
    _chat(chat, "[Mon 2026-10-05 02:00 UTC] Question?\n\n[DATABASE NOTES]\nNote 1")

    assert delivered(chat, "[DATABASE NOTES]\nNote 1\n") == (True, "")


@pytest.mark.parametrize("user_text, reason", [
    ("[Mon 2026-10-05 02:00 UTC] Question?", "first user message does not contain the notes"),
    (None, "chat.jsonl not found"),
])
def test_delivery_failures_name_the_reason(tmp_path, user_text, reason):
    chat = tmp_path / "chat.jsonl"
    if user_text is not None:
        _chat(chat, user_text)

    assert delivered(chat, "[DATABASE NOTES]\nNote 1\n") == (False, reason)


def test_failed_delivery_is_recorded_and_raises(tmp_path):
    rules = _rules_dir(tmp_path)

    with pytest.raises(DeliveryError, match="task_009"):
        _run(_FakeRunBatch(tmp_path / "out", deliver=False), rules, tmp_path)

    out = tmp_path / "out/task_009/gpt-5.1_x_abcdef"
    receipt = json.loads((out / "knowledge_receipt.json").read_text("utf-8"))
    assert receipt["delivery_ok"] is False
    assert receipt["delivery_reason"] == "first user message does not contain the notes"
    failures = [json.loads(l) for l in (tmp_path / "out/self_inject_failures.jsonl").read_text("utf-8").splitlines()]
    assert failures == [{"task_id": "task_009", "output_dir": str(out),
                         "reason": "first user message does not contain the notes"}]


def test_missing_rules_lists_tasks_without_a_rule_file(tmp_path):
    rules = _rules_dir(tmp_path)

    assert missing_rules(rules, ["task_009", "task_352"]) == ["task_352"]


def _scored_run(root, task_id, suffix, score):
    out = root / task_id / suffix
    out.mkdir(parents=True)
    (out / "score.json").write_text(json.dumps({"task_id": task_id, "score": score}), encoding="utf-8")
    return out


def test_summary_pairs_the_latest_bare_and_injected_scores_per_task(tmp_path):
    rules = _rules_dir(tmp_path)
    bare, injected = tmp_path / "bare", tmp_path / "inject"
    _scored_run(bare, "task_009", "gpt-5.1_20261005_0900_aaaaaa", 1.0)
    _scored_run(bare, "task_009", "gpt-5.1_20261005_1000_bbbbbb", 0.0)
    _run(_FakeRunBatch(injected), rules, tmp_path)
    (injected / "task_009/gpt-5.1_x_abcdef/score.json").write_text(json.dumps({"score": 1.0}), encoding="utf-8")

    assert summarize(bare, injected, rules) == [{
        "task_id": "task_009", "basis": ["data"], "bare_score": 0.0, "injected_score": 1.0,
        "delivery_ok": True, "delivery_reason": "",
    }]


def test_summary_does_not_count_an_undelivered_or_missing_run(tmp_path):
    rules = _rules_dir(tmp_path)
    injected = tmp_path / "inject"
    out = _scored_run(injected, "task_009", "gpt-5.1_x_abcdef", 1.0)
    _chat(out / "chat.jsonl", "Question?")
    notes = (rules / "task_009.md").read_text(encoding="utf-8")
    (out / "knowledge_receipt.json").write_text(json.dumps({"injected_text": notes}), encoding="utf-8")

    assert summarize(tmp_path / "bare", injected, rules) == [{
        "task_id": "task_009", "basis": ["data"], "bare_score": None, "injected_score": None,
        "delivery_ok": False, "delivery_reason": "first user message does not contain the notes",
    }]
