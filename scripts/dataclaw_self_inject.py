#!/usr/bin/env python3
"""Self-injection test of accepted DataClaw rules (TK-Boost-adapt.md 9.6).

    # rule files from one (the latest) reflector round
    python scripts/dataclaw_self_inject.py build-rules --reflect-dir tmp/dataclaw_dev/reflect/rev2_gpt51_low

    # injected arm; the bare arm uses DataClaw's run_batch.py directly
    python scripts/dataclaw_self_inject.py run --suite task_009_...,task_011_...

    python scripts/dataclaw_self_inject.py report
"""

import argparse
import importlib
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tkstore.dataclaw.self_inject import (  # noqa: E402
    DeliveryError,
    build_rules,
    missing_rules,
    run_injected,
    summarize,
)

DEFAULT_RULES_DIR = _REPO_ROOT / "data/dataclaw_dev/self_inject/rules"
DEFAULT_DATACLAW_ROOT = Path.home() / "DataClaw"
DEFAULT_OUTPUT_SUBDIR = "output_rules_self_inject"
DEFAULT_BARE_SUBDIR = "output_rules_self_bare"


def load_run_batch(dataclaw_root: Path, output_subdir: str):
    """Import DataClaw's run_batch with OUTPUT_DIR pointing at ``output_subdir``.

    run_batch reads OUTPUT_SUBDIR at import time, and its load_dotenv() does
    not override a variable that is already set.
    """
    os.environ["OUTPUT_SUBDIR"] = output_subdir
    os.chdir(dataclaw_root)
    sys.path.insert(0, str(dataclaw_root))
    return importlib.import_module("dataclaw.eval.run_batch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-rules", help="write one rule file per task from one reflector round")
    build.add_argument("--reflect-dir", type=Path, required=True,
                       help="directory of the latest reflector round; only its own *.json are read")
    build.add_argument("--out", type=Path, default=DEFAULT_RULES_DIR)

    run = sub.add_parser("run", help="run the injected arm through DataClaw's run_single_task")
    run.add_argument("--suite", required=True, help="comma-separated task ids")
    run.add_argument("--rules-dir", type=Path, default=DEFAULT_RULES_DIR)
    run.add_argument("--dataclaw-root", type=Path, default=DEFAULT_DATACLAW_ROOT)
    run.add_argument("--output-subdir", default=DEFAULT_OUTPUT_SUBDIR)
    run.add_argument("--model", help="agent model (default: DataClaw's DEFAULT_MODEL)")
    run.add_argument("--judge", help="judge model (default: DataClaw's JUDGE_MODEL)")
    run.add_argument("--timeout-multiplier", type=float)

    report = sub.add_parser("report", help="compare the bare and injected arms")
    report.add_argument("--rules-dir", type=Path, default=DEFAULT_RULES_DIR)
    report.add_argument("--bare", type=Path, default=DEFAULT_DATACLAW_ROOT / DEFAULT_BARE_SUBDIR)
    report.add_argument("--injected", type=Path, default=DEFAULT_DATACLAW_ROOT / DEFAULT_OUTPUT_SUBDIR)
    return parser


def cmd_build_rules(args) -> int:
    sets = build_rules(args.reflect_dir.resolve(), args.out.resolve())
    print(f"{len(sets)} rule files from {args.reflect_dir} -> {args.out}")
    for task_id, rs in sets.items():
        print(f"  {task_id}: {', '.join(rs.ids)} ({', '.join(rs.basis)})")
    return 0


def cmd_run(args, load) -> int:
    task_ids = [t.strip() for t in args.suite.split(",") if t.strip()]
    rules_dir = args.rules_dir.resolve()
    missing = missing_rules(rules_dir, task_ids)
    if missing:
        print(f"no rule file for: {', '.join(missing)}", file=sys.stderr)
        return 2

    run_batch = load(args.dataclaw_root.resolve(), args.output_subdir)
    tasks = {t.task_id: t for t in run_batch.TaskLoader(run_batch.TASKS_DIR).load_all_tasks()}
    unknown = [t for t in task_ids if t not in tasks]
    if unknown:
        print(f"unknown task ids: {', '.join(unknown)}", file=sys.stderr)
        return 2

    model = args.model or run_batch.DEFAULT_MODEL
    judge = args.judge or run_batch.DEFAULT_JUDGE_MODEL
    timeout = args.timeout_multiplier or getattr(run_batch, "TIMEOUT_MULTIPLIER", 1.0)
    failures = Path(run_batch.OUTPUT_DIR) / "self_inject_failures.jsonl"
    for i, task_id in enumerate(task_ids, 1):
        print(f"[{i}/{len(task_ids)}] {task_id}", flush=True)
        try:
            result = run_injected(run_batch, tasks[task_id], rules_dir, model=model, judge_model=judge,
                                  timeout_multiplier=timeout, failures_path=failures)
        except DeliveryError as exc:
            print(f"notes not delivered, stopping: {exc}; recorded in {failures}", file=sys.stderr)
            return 1
        print(f"  {result['output_dir']} error={result.get('error')}", flush=True)
    return 0


def cmd_report(args) -> int:
    rows = summarize(args.bare, args.injected, args.rules_dir)
    print("task_id\tbasis\tbare\tinjected\tdelivered")
    for r in rows:
        delivered = "yes" if r["delivery_ok"] else f"no ({r['delivery_reason']})"
        print(f"{r['task_id']}\t{','.join(r['basis'])}\t{r['bare_score']}\t{r['injected_score']}\t{delivered}")
    return 0


def main(argv=None, load_run_batch=load_run_batch) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build-rules":
        return cmd_build_rules(args)
    if args.command == "run":
        return cmd_run(args, load_run_batch)
    return cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
