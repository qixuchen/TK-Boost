"""Accepted divergences from reflector records, stored as stage C input.

One JSON line per accepted divergence. ``divergence_id`` is
``<task_id[:8]>__<run_dir[-6:]>__<index>``; the same run accepted in two
reflector records gets ``~2``, ``~3`` ... on the later ones (ordered by path).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .reflector_io import Divergence, Evidence, Reproduction
from .scope import ColumnRef


@dataclass
class DivergenceInput:
    divergence_id: str
    task_id: str
    run_dir: str
    source_record: str
    model: str
    divergence: Divergence


def export_accepted(record_paths: list[Path], *, root: Path) -> list[dict]:
    root = Path(root)
    relative = sorted((Path(p).resolve().relative_to(root.resolve()).as_posix(), Path(p)) for p in record_paths)
    records: list[dict] = []
    seen: dict[str, int] = {}
    for rel, path in relative:
        record = json.loads(path.read_text(encoding="utf-8"))
        for accepted in record.get("accepted") or []:
            d = accepted["divergence"]
            base = f"{record['task_id'][:8]}__{record['run_dir'][-6:]}__{d['index']}"
            seen[base] = seen.get(base, 0) + 1
            records.append({
                "divergence_id": base if seen[base] == 1 else f"{base}~{seen[base]}",
                "task_id": record["task_id"],
                "run_dir": record["run_dir"],
                "source_record": rel,
                "model": record.get("model", ""),
                "divergence": d,
            })
    return records


def write_divergences(records: list[dict], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )


def _divergence(data: dict) -> Divergence:
    return Divergence(
        index=data["index"],
        divergence=data.get("divergence", ""),
        needed=data.get("needed", ""),
        scope=data.get("scope", ""),
        tables=list(data.get("tables") or []),
        columns=[ColumnRef(*c) for c in data.get("columns") or []],
        fact=data.get("fact", ""),
        category=data.get("category", ""),
        evidence=[Evidence(e["probe"], e["start"], e["end"], e["excerpt"]) for e in data.get("evidence") or []],
        reproduced=[
            Reproduction(r["key"], r["value"], r["probe"], r["start"], r["end"], r.get("semantic_match"),
                         r.get("excerpt", ""))
            for r in data.get("reproduced") or []
        ],
        kind=data.get("kind", ""),
        errors=list(data.get("errors") or []),
    )


def load_divergences(path: Path) -> list[DivergenceInput]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        items.append(DivergenceInput(
            divergence_id=r["divergence_id"],
            task_id=r["task_id"],
            run_dir=r["run_dir"],
            source_record=r["source_record"],
            model=r.get("model", ""),
            divergence=_divergence(r["divergence"]),
        ))
    return items
