"""Loading the DataClaw development set described by manifest.csv."""

import csv
import hashlib
import json

import pytest

from tkstore.dataclaw.devset import is_failed, load_manifest, load_run, parse_task_markdown

TASK_MD = """---
id: task_001_demo_easy_easy001
category: demo
gold_file: qa_gold/demo/easy001.json
---

# Demo

## Prompt

Which province leads?

Output guidelines:
Answer with a province name.

## Expected Behavior

Agent reads files.

## Grading Criteria

- [ ] correct
"""

FIELDS = [
    "task_id", "run_dir", "category", "level", "score", "gpr",
    "chat_bytes", "chat_sha256", "source_dir", "task_file", "gold_file",
]


def _write_run(root, task_id, run_dir, chat_text, *, process=True, score=0.0):
    run_path = root / "runs" / task_id / run_dir
    run_path.mkdir(parents=True)
    (run_path / "chat.jsonl").write_text(chat_text, encoding="utf-8")
    (run_path / "score.json").write_text(
        json.dumps({"task_id": task_id, "score": score, "max_score": 1.0, "notes": "wrong"}),
        encoding="utf-8",
    )
    if process:
        (run_path / "process_score.json").write_text(
            json.dumps({"gpr": {"gpr": 0.0, "break_point": 0}}), encoding="utf-8"
        )
    return run_path


@pytest.fixture
def dev_root(tmp_path):
    root = tmp_path / "dataclaw_dev"
    task_file = tmp_path / "task.md"
    task_file.write_text(TASK_MD, encoding="utf-8")
    gold_file = tmp_path / "gold.json"
    gold_file.write_text(
        json.dumps({"answer": "Guangdong", "milestone": {"Top province": "Guangdong"}}),
        encoding="utf-8",
    )
    chat = json.dumps(
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Which province leads?"}]}}
    ) + "\n"
    rows = []
    for run_dir, process in (("glm-5.2_a", True), ("glm-5.2_b", False)):
        _write_run(root, "task_001_demo_easy_easy001", run_dir, chat, process=process)
        rows.append({
            "task_id": "task_001_demo_easy_easy001", "run_dir": run_dir,
            "category": "demo", "level": "easy", "score": "0.0", "gpr": "0.0",
            "chat_bytes": str(len(chat.encode())),
            "chat_sha256": hashlib.sha256(chat.encode()).hexdigest(),
            "source_dir": "/archive/somewhere", "task_file": str(task_file),
            "gold_file": str(gold_file),
        })
    with (root / "manifest.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return root


def test_parse_task_markdown_takes_only_the_prompt_section(tmp_path):
    path = tmp_path / "task.md"
    path.write_text(TASK_MD, encoding="utf-8")
    meta, prompt = parse_task_markdown(path)
    assert meta["id"] == "task_001_demo_easy_easy001"
    assert prompt == "Which province leads?\n\nOutput guidelines:\nAnswer with a province name."


def test_parse_task_markdown_requires_frontmatter(tmp_path):
    path = tmp_path / "task.md"
    path.write_text("## Prompt\n\nno frontmatter\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_task_markdown(path)


def test_load_manifest_resolves_run_dirs_under_runs(dev_root):
    runs = load_manifest(dev_root / "manifest.csv")
    assert [r.run_dir.name for r in runs] == ["glm-5.2_a", "glm-5.2_b"]
    assert runs[0].run_dir == dev_root / "runs" / "task_001_demo_easy_easy001" / "glm-5.2_a"
    assert runs[0].category == "demo" and runs[0].level == "easy"


def test_load_run_reads_everything(dev_root):
    first, second = (load_run(r) for r in load_manifest(dev_root / "manifest.csv"))
    assert first.prompt == "Which province leads?\n\nOutput guidelines:\nAnswer with a province name."
    assert first.gold["milestone"] == {"Top province": "Guangdong"}
    assert first.score["notes"] == "wrong"
    assert first.process_score == {"gpr": {"gpr": 0.0, "break_point": 0}}
    assert first.trajectory.prompt == "Which province leads?"
    assert second.process_score is None


def test_load_run_rejects_changed_chat(dev_root):
    run = load_manifest(dev_root / "manifest.csv")[0]
    (run.run_dir / "chat.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        load_run(run)


@pytest.mark.parametrize(
    "score, failed",
    [
        ({"score": 0.0, "max_score": 1.0}, True),
        ({"score": 0.5, "max_score": 1.0}, True),
        ({"score": 1.0, "max_score": 1.0}, False),
        ({"score": 1.0}, False),
        ({"score": 0}, True),
    ],
)
def test_is_failed(score, failed):
    assert is_failed(score) is failed
