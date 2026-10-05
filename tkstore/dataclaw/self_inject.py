"""Self-injection test (TK-Boost-adapt.md 9.6).

Accepted rules of one reflector round are rendered into one notes block per
task. The block is appended to the agent's prompt only; the judges keep the
original task prompt.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .divergence_store import export_accepted

NOTES_HEADER = """[DATABASE NOTES]
The notes below come from earlier analyses of this same database. They describe
pitfalls in the data files, not answers to the question above. For each note:
1. Read "Applies when". If it does not describe the question above, ignore the note.
2. If it applies, carry out "Check" before computing the quantity it concerns.
3. "Why" states the data property behind the note; verify it in the data if in doubt.
4. If the question explicitly requires something different, follow the question."""

NOTES_END = "[END DATABASE NOTES]"
NOTES_FOOTER = "The answer must still follow the output guidelines in the question above."


@dataclass
class RuleSet:
    task_id: str
    ids: list[str] = field(default_factory=list)
    basis: list[str] = field(default_factory=list)
    divergences: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return render_notes(self.divergences)


def render_notes(divergences: list[dict]) -> str:
    notes = [
        f"Note {i}\nApplies when: {d['when_to_check']}\nCheck: {d['ensure']}\nWhy: {d['context']}"
        for i, d in enumerate(divergences, 1)
    ]
    return f"{NOTES_HEADER}\n\n" + "\n\n".join(notes) + f"\n{NOTES_END}\n\n{NOTES_FOOTER}\n"


def rule_sets(exported: list[dict]) -> dict[str, RuleSet]:
    sets: dict[str, RuleSet] = {}
    for record in exported:
        rs = sets.setdefault(record["task_id"], RuleSet(record["task_id"]))
        rs.ids.append(record["divergence_id"])
        rs.basis.append(record["divergence"].get("basis", ""))
        rs.divergences.append(record["divergence"])
    return sets


def build_rules(reflect_dir: Path, out_dir: Path) -> dict[str, RuleSet]:
    """Write one ``<task_id>.md`` per task from the records directly in ``reflect_dir``."""
    reflect_dir, out_dir = Path(reflect_dir), Path(out_dir)
    sets = rule_sets(export_accepted(sorted(reflect_dir.glob("*.json")), root=reflect_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in [*out_dir.glob("*.md"), out_dir / "index.json"]:
        old.unlink(missing_ok=True)
    tasks = {}
    for task_id, rs in sets.items():
        (out_dir / f"{task_id}.md").write_text(rs.text, encoding="utf-8")
        tasks[task_id] = {"file": f"{task_id}.md", "ids": rs.ids, "basis": rs.basis}
    (out_dir / "index.json").write_text(
        json.dumps({"source": str(reflect_dir), "tasks": tasks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return sets


def agent_prompt(task_prompt: str, notes: str) -> str:
    return f"{task_prompt}\n\n{notes}"


class DeliveryError(RuntimeError):
    pass


def _index(rules_dir: Path) -> dict:
    return json.loads((Path(rules_dir) / "index.json").read_text(encoding="utf-8"))["tasks"]


def missing_rules(rules_dir: Path, task_ids: list[str]) -> list[str]:
    tasks = _index(rules_dir)
    return [t for t in task_ids if t not in tasks or not (Path(rules_dir) / tasks[t]["file"]).is_file()]


def _first_user_text(chat_path: Path) -> str | None:
    for line in chat_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        message = entry.get("message", entry)
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))
    return None


def delivered(chat_path: Path, notes: str) -> tuple[bool, str]:
    """Whether the first user message of the agent transcript contains ``notes``.

    OpenClaw prefixes the message with a timestamp and the shell drops trailing
    newlines, so the check is containment of the stripped notes.
    """
    chat_path = Path(chat_path)
    if not chat_path.is_file():
        return False, "chat.jsonl not found"
    text = _first_user_text(chat_path)
    if text is None:
        return False, "no user message in chat.jsonl"
    if notes.strip() not in text:
        return False, "first user message does not contain the notes"
    return True, ""


def run_injected(run_batch, task, rules_dir: Path, *, model: str, judge_model: str,
                 timeout_multiplier: float, failures_path: Path) -> dict:
    """Run one task through ``run_batch.run_single_task`` with the notes in the agent prompt only.

    ``run_single_task`` sends ``task.prompt`` both to the agent and to
    ``grade_task``; ``grade_task`` is swapped for the run so the judge sees the
    original prompt. Runs one task at a time: the swap is not thread-safe.
    """
    rules_dir = Path(rules_dir)
    meta = _index(rules_dir)[task.task_id]
    rule_file = rules_dir / meta["file"]
    notes = rule_file.read_text(encoding="utf-8")
    original_prompt = task.prompt
    injected = copy.copy(task)
    injected.prompt = agent_prompt(original_prompt, notes)

    real_grade = run_batch.grade_task

    def grade_with_original_prompt(*args, **kwargs):
        kwargs["task_prompt"] = original_prompt
        return real_grade(*args, **kwargs)

    run_batch.grade_task = grade_with_original_prompt
    try:
        result = run_batch.run_single_task(injected, model, judge_model, timeout_multiplier)
    finally:
        run_batch.grade_task = real_grade

    output_dir = Path(result["output_dir"])
    ok, reason = delivered(output_dir / "chat.jsonl", notes)
    digest = hashlib.sha256(notes.encode("utf-8")).hexdigest()
    receipt = {
        "rule_file": str(rule_file),
        "rule_file_sha256": hashlib.sha256(rule_file.read_bytes()).hexdigest(),
        "rule_ids": meta["ids"],
        "basis": meta["basis"],
        "injected_text": notes,
        "injected_sha256": digest,
        "injected_chars": len(notes),
        "delivery_ok": ok,
        "delivery_reason": reason,
    }
    (output_dir / "knowledge_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["knowledge_receipt"] = receipt
    if not ok:
        failures_path = Path(failures_path)
        failures_path.parent.mkdir(parents=True, exist_ok=True)
        with failures_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"task_id": task.task_id, "output_dir": str(output_dir), "reason": reason},
                               ensure_ascii=False) + "\n")
        raise DeliveryError(f"{task.task_id}: {reason} ({output_dir})")
    return result


def _latest_run(root: Path, task_id: str) -> Path | None:
    runs = sorted(p for p in (Path(root) / task_id).glob("*") if p.is_dir())
    return runs[-1] if runs else None


def _score(run: Path | None) -> float | None:
    if run is None or not (run / "score.json").is_file():
        return None
    return json.loads((run / "score.json").read_text(encoding="utf-8")).get("score")


def summarize(bare_root: Path, injected_root: Path, rules_dir: Path) -> list[dict]:
    """One row per rule-bearing task: latest bare and injected scores; an undelivered injected run scores None."""
    rows = []
    for task_id, meta in _index(rules_dir).items():
        run = _latest_run(injected_root, task_id)
        if run is None:
            ok, reason = False, "no injected run"
        elif not (run / "knowledge_receipt.json").is_file():
            ok, reason = False, "knowledge_receipt.json not found"
        else:
            receipt = json.loads((run / "knowledge_receipt.json").read_text(encoding="utf-8"))
            ok, reason = delivered(run / "chat.jsonl", receipt["injected_text"])
        rows.append({
            "task_id": task_id,
            "basis": meta["basis"],
            "bare_score": _score(_latest_run(bare_root, task_id)),
            "injected_score": _score(run) if ok else None,
            "delivery_ok": ok,
            "delivery_reason": reason,
        })
    return rows
