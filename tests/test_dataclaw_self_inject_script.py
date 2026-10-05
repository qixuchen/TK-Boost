"""Subcommands of scripts/dataclaw_self_inject.py."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dataclaw_self_inject.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("dataclaw_self_inject", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _round(tmp_path, task_ids):
    reflect = tmp_path / "reflect"
    reflect.mkdir()
    for i, task_id in enumerate(task_ids):
        d = {"index": 1, "when_to_check": f"W{i}", "ensure": f"E{i}", "context": f"C{i}", "basis": "data"}
        (reflect / f"{task_id}__run.json").write_text(json.dumps(
            {"task_id": task_id, "run_dir": f"glm-5.2_x_{i:06d}", "accepted": [{"divergence": d}]}), encoding="utf-8")
    return reflect


class _FakeRunBatch:
    def __init__(self, root, task_ids, deliver=True):
        self.OUTPUT_DIR = root / "out"
        self.TASKS_DIR = root / "tasks"
        self.DEFAULT_MODEL = "gpt-5.1"
        self.DEFAULT_JUDGE_MODEL = "judge"
        self.deliver = deliver
        self.ran = []
        tasks = [SimpleNamespace(task_id=t, prompt=f"Q {t}") for t in task_ids]
        self.TaskLoader = lambda _dir: SimpleNamespace(load_all_tasks=lambda: tasks)
        self.grade_task = lambda **kwargs: None

    def run_single_task(self, task, model, judge_model, timeout_multiplier, skill_path=None):
        self.ran.append((task.task_id, model, judge_model))
        out = self.OUTPUT_DIR / task.task_id / "gpt-5.1_x_abcdef"
        out.mkdir(parents=True)
        text = task.prompt if self.deliver else "Q"
        (out / "chat.jsonl").write_text(json.dumps(
            {"message": {"role": "user", "content": [{"type": "text", "text": text}]}}) + "\n", encoding="utf-8")
        self.grade_task(task_prompt=task.prompt)
        return {"task_id": task.task_id, "output_dir": str(out)}


def _build(script, tmp_path, task_ids):
    rules = tmp_path / "rules"
    assert script.main(["build-rules", "--reflect-dir", str(_round(tmp_path, task_ids)), "--out", str(rules)]) == 0
    return rules


def test_run_stops_before_any_task_when_a_rule_file_is_missing(script, tmp_path, capsys):
    rules = _build(script, tmp_path, ["task_009"])
    loaded = []

    code = script.main(["run", "--rules-dir", str(rules), "--suite", "task_009,task_352"],
                       load_run_batch=lambda *a: loaded.append(a))

    assert code != 0
    assert loaded == []
    assert "task_352" in capsys.readouterr().err


def test_run_injects_each_task_with_the_runner_defaults(script, tmp_path):
    rules = _build(script, tmp_path, ["task_009", "task_011"])
    fake = _FakeRunBatch(tmp_path, ["task_009", "task_011", "task_218"])

    code = script.main(["run", "--rules-dir", str(rules), "--suite", "task_009,task_011",
                        "--output-subdir", "output_rules_self_inject"],
                       load_run_batch=lambda root, subdir: fake if subdir == "output_rules_self_inject" else None)

    assert code == 0
    assert fake.ran == [("task_009", "gpt-5.1", "judge"), ("task_011", "gpt-5.1", "judge")]
    assert (fake.OUTPUT_DIR / "task_009/gpt-5.1_x_abcdef/knowledge_receipt.json").is_file()


def test_run_exits_nonzero_at_the_first_undelivered_task(script, tmp_path, capsys):
    rules = _build(script, tmp_path, ["task_009", "task_011"])
    fake = _FakeRunBatch(tmp_path, ["task_009", "task_011"], deliver=False)

    code = script.main(["run", "--rules-dir", str(rules), "--suite", "task_009,task_011"],
                       load_run_batch=lambda *a: fake)

    assert code != 0
    assert [r[0] for r in fake.ran] == ["task_009"]
    assert (fake.OUTPUT_DIR / "self_inject_failures.jsonl").is_file()
    assert "task_009" in capsys.readouterr().err
