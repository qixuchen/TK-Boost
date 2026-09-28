"""Load the DataClaw development set listed in ``data/dataclaw_dev/manifest.csv``.

Each manifest row is one archived bare-OpenClaw run copied to
``<manifest dir>/runs/<task_id>/<run_dir>/``; task and gold files are read from
their original DataClaw paths.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .trajectory import Trajectory, parse_chat

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SECTION = re.compile(r"^##\s+(.+)$")


@dataclass(frozen=True)
class DevRun:
    task_id: str
    run_dir: Path
    category: str
    level: str
    chat_sha256: str
    task_file: Path
    gold_file: Path


@dataclass
class LoadedRun:
    run: DevRun
    prompt: str
    gold: dict[str, Any]
    score: dict[str, Any]
    process_score: dict[str, Any] | None
    trajectory: Trajectory


def parse_task_markdown(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, prompt), splitting sections the way DataClaw's lib_tasks does."""
    match = _FRONTMATTER.match(Path(path).read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"No YAML frontmatter found in {path}")
    meta = yaml.safe_load(match.group(1)) or {}

    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in match.group(2).split("\n"):
        header = _SECTION.match(line)
        if header:
            current = header.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return meta, "\n".join(sections.get("Prompt", [])).strip()


def load_manifest(manifest_path: Path, runs_root: Path | None = None) -> list[DevRun]:
    manifest_path = Path(manifest_path)
    runs_root = Path(runs_root) if runs_root else manifest_path.parent / "runs"
    with manifest_path.open(newline="", encoding="utf-8") as fh:
        return [
            DevRun(
                task_id=row["task_id"],
                run_dir=runs_root / row["task_id"] / row["run_dir"],
                category=row["category"],
                level=row["level"],
                chat_sha256=row["chat_sha256"],
                task_file=Path(row["task_file"]),
                gold_file=Path(row["gold_file"]),
            )
            for row in csv.DictReader(fh)
        ]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_run(run: DevRun) -> LoadedRun:
    chat_path = run.run_dir / "chat.jsonl"
    digest = hashlib.sha256(chat_path.read_bytes()).hexdigest()
    if digest != run.chat_sha256:
        raise ValueError(
            f"{chat_path}: sha256 {digest} does not match manifest {run.chat_sha256}"
        )
    process_path = run.run_dir / "process_score.json"
    _, prompt = parse_task_markdown(run.task_file)
    return LoadedRun(
        run=run,
        prompt=prompt,
        gold=_read_json(run.gold_file),
        score=_read_json(run.run_dir / "score.json"),
        process_score=_read_json(process_path) if process_path.is_file() else None,
        trajectory=parse_chat(chat_path),
    )


def is_failed(score: dict[str, Any]) -> bool:
    """Outcome gate: only runs the judge scored below full marks go to reflection."""
    return float(score["score"]) < float(score.get("max_score", 1.0))
