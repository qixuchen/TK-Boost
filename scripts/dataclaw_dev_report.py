#!/usr/bin/env python3
"""Smoke-check stage A of the DataClaw adaptation on the real development set.

Loads every run in data/dataclaw_dev/manifest.csv (verifying chat sha256),
applies the outcome gate, compresses each trajectory, builds the database
catalog, and writes a length report plus two sample compressed trajectories to
tmp/dataclaw_dev/. Depends on machine-local data, so it lives here rather than
in `tests/`.

    python scripts/dataclaw_dev_report.py [--max-output-chars 2000] [--max-thinking-chars 5000]
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tkstore.dataclaw.catalog import load_catalog  # noqa: E402
from tkstore.dataclaw.devset import is_failed, load_manifest, load_run  # noqa: E402
from tkstore.dataclaw.trajectory import compress, length_stats  # noqa: E402

DEFAULT_DATA_DIR = Path.home() / "DataClaw" / "assets" / "database"


def _fmt(stats: dict) -> str:
    return ", ".join(f"{k} {v:,}" for k, v in stats.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=_REPO_ROOT / "data/dataclaw_dev/manifest.csv")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "tmp/dataclaw_dev")
    parser.add_argument("--max-output-chars", type=int, default=2000)
    parser.add_argument("--max-thinking-chars", type=int, default=5000)
    parser.add_argument("--no-thinking", action="store_true")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    runs = load_manifest(args.manifest)
    rows = []
    problems = []
    for run in runs:
        try:
            loaded = load_run(run)
        except Exception as exc:  # report every broken run, not just the first
            problems.append(f"{run.task_id}/{run.run_dir.name}: {exc}")
            continue
        text = compress(
            loaded.trajectory,
            max_output_chars=args.max_output_chars,
            max_thinking_chars=args.max_thinking_chars,
            keep_thinking=not args.no_thinking,
        )
        calls = sum(len(s.calls) for s in loaded.trajectory.steps)
        unanswered = sum(1 for s in loaded.trajectory.steps for c in s.calls if c.result is None)
        rows.append({
            "run": loaded,
            "failed": is_failed(loaded.score),
            "raw_bytes": (run.run_dir / "chat.jsonl").stat().st_size,
            "compressed": text,
            "steps": len(loaded.trajectory.steps),
            "calls": calls,
            "unanswered": unanswered,
            "prompt_match": loaded.trajectory.prompt.strip() == loaded.prompt.strip(),
            "milestones": len(loaded.gold.get("milestone") or {}),
            "has_process": loaded.process_score is not None,
        })

    emit("# DataClaw stage A development-set report")
    emit()
    emit(f"compression: max_output_chars={args.max_output_chars}, "
         f"max_thinking_chars={args.max_thinking_chars}, keep_thinking={not args.no_thinking}")
    emit(f"runs in manifest: {len(runs)}; loaded with matching sha256: {len(rows)}; "
         f"tasks: {len({r['run'].run.task_id for r in rows})}")
    emit(f"outcome gate says failed: {sum(r['failed'] for r in rows)} / {len(rows)}")
    emit(f"chat prompt equals task prompt: {sum(r['prompt_match'] for r in rows)} / {len(rows)}")
    emit(f"with process_score.json: {sum(r['has_process'] for r in rows)} / {len(rows)}")
    emit(f"tool calls without a result: {sum(r['unanswered'] for r in rows)} "
         f"of {sum(r['calls'] for r in rows)}")
    for problem in problems:
        emit(f"LOAD ERROR {problem}")
    emit()
    emit(f"raw chat.jsonl bytes: {_fmt(length_stats([r['raw_bytes'] for r in rows]))}")
    emit(f"compressed chars:     {_fmt(length_stats([len(r['compressed']) for r in rows]))}")
    emit(f"steps per run:        {_fmt(length_stats([r['steps'] for r in rows]))}")
    emit(f"tool calls per run:   {_fmt(length_stats([r['calls'] for r in rows]))}")
    emit()
    emit("| task_id | run_dir | failed | steps | calls | raw bytes | compressed chars | milestones |")
    emit("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in sorted(rows, key=lambda r: len(r["compressed"])):
        run = r["run"].run
        emit(f"| {run.task_id} | {run.run_dir.name} | {r['failed']} | {r['steps']} | {r['calls']} "
             f"| {r['raw_bytes']:,} | {len(r['compressed']):,} | {r['milestones']} |")

    started = time.time()
    catalog = load_catalog(args.data_dir, _REPO_ROOT / "tmp/dataclaw_cache")
    emit()
    emit(f"catalog: {len(catalog.headers)} files, "
         f"{sum(len(c) for c in catalog.headers.values())} columns, "
         f"{len(catalog.values):,} cell values of 3-40 chars ({time.time() - started:.1f}s)")

    if rows:
        by_size = sorted(rows, key=lambda r: len(r["compressed"]))
        samples = {"median": by_size[len(by_size) // 2], "largest": by_size[-1]}
        emit()
        for label, r in samples.items():
            run = r["run"].run
            path = args.out_dir / f"sample_{label}_{run.task_id}_{run.run_dir.name}.txt"
            path.write_text(
                f"PROMPT:\n{r['run'].prompt}\n\nGOLD ANSWER: {r['run'].gold.get('answer')}\n\n"
                f"{r['compressed']}",
                encoding="utf-8",
            )
            emit(f"sample ({label}): {path.relative_to(_REPO_ROOT)}")

    (args.out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if not problems and len(rows) == len(runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
