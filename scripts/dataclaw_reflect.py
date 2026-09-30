#!/usr/bin/env python3
"""Run the DataClaw reflector on development runs (stages B and C).

With --dry-run it only writes the messages the reflector would receive, so the
prompt and inputs can be reviewed and their size checked; no LLM, no container.
Otherwise each run gets its own probe container and one JSON record in --out-dir.

    python scripts/dataclaw_reflect.py --dry-run
    python scripts/dataclaw_reflect.py --model <litellm model id>
"""

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tkstore.dataclaw.catalog import load_catalog  # noqa: E402
from tkstore.dataclaw.devset import load_manifest, load_run  # noqa: E402
from tkstore.dataclaw.probe import DockerExecutor, ProbeSession  # noqa: E402
from tkstore.dataclaw.reflector import (  # noqa: E402
    ReflectorConfig,
    build_messages,
    litellm_llm,
    reflect,
    to_record,
)

PILOT_TASKS = ("task_049_", "task_218_", "task_195_", "task_206_", "task_011_")
DEFAULT_DATA_DIR = Path.home() / "DataClaw" / "assets" / "database"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=_REPO_ROOT / "data/dataclaw_dev/manifest.csv")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "tmp/dataclaw_dev/reflect")
    parser.add_argument("--tasks", default=",".join(PILOT_TASKS),
                        help="comma-separated task_id prefixes (default: the five pilot tasks)")
    parser.add_argument("--model", help="litellm model id; required unless --dry-run")
    parser.add_argument("--judge-model", help="litellm model id of the generality judge (default: --model)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-probes", type=int, default=20)
    parser.add_argument("--max-finals", type=int, default=3)
    parser.add_argument("--probe-timeout", type=int, default=60)
    parser.add_argument("--max-probe-chars", type=int, default=4000)
    parser.add_argument("--max-tokens", type=int, help="output token cap per LLM call (reasoning included)")
    parser.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high", "omit"), default="low",
                        help="sent as extra_body.reasoning_effort (omit: not sent); honoured by gpt-5.1, "
                             "ignored by glm-5.2 on the current gateway")
    parser.add_argument("--disable-thinking", action="store_true",
                        help='sent as extra_body.thinking = {"type": "disabled"}; the gateway may ignore it')
    parser.add_argument("--call-timeout", type=float, default=300,
                        help="wall-clock cap in seconds for one LLM call")
    parser.add_argument("--run-time-budget", type=float, default=1500,
                        help="seconds per run before the reflector is told to finish")
    return parser


def _llm_options(args: argparse.Namespace) -> dict:
    return {
        "max_tokens": args.max_tokens,
        "reasoning_effort": None if args.reasoning_effort == "omit" else args.reasoning_effort,
        "disable_thinking": args.disable_thinking,
        "call_timeout_s": args.call_timeout,
    }


def make_llms(args: argparse.Namespace, factory=litellm_llm):
    """The reflector LLM and the judge LLM (None: the judge uses the reflector's)."""
    if args.dry_run:
        return None, None
    llm = factory(args.model, **_llm_options(args))
    judge_llm = factory(args.judge_model, **_llm_options(args)) if args.judge_model else None
    return llm, judge_llm


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.dry_run and not args.model:
        parser.error("--model is required unless --dry-run")
    args.out_dir = args.out_dir.resolve()

    prefixes = tuple(p.strip() for p in args.tasks.split(",") if p.strip())
    runs = [r for r in load_manifest(args.manifest) if r.task_id.startswith(prefixes)]
    catalog = load_catalog(args.data_dir, _REPO_ROOT / "tmp/dataclaw_cache")
    config = ReflectorConfig(
        max_probes=args.max_probes,
        max_finals=args.max_finals,
        run_time_budget_s=args.run_time_budget,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    llm_options = _llm_options(args)
    llm, judge_llm = make_llms(args)

    for run in runs:
        loaded = load_run(run)
        stem = f"{run.task_id}__{run.run_dir.name}"
        if args.dry_run:
            messages = build_messages(loaded, catalog, config)
            path = args.out_dir / f"{stem}.messages.md"
            path.write_text(
                "\n\n".join(f"===== {m['role']} =====\n{m['content']}" for m in messages),
                encoding="utf-8",
            )
            chars = sum(len(m["content"]) for m in messages)
            print(f"{stem}: {chars:,} chars -> {path.relative_to(_REPO_ROOT)}")
            continue

        path = args.out_dir / f"{stem}.json"
        try:
            executor = DockerExecutor(args.data_dir)
            with ProbeSession(executor, timeout=args.probe_timeout, max_llm_chars=args.max_probe_chars) as session:
                result = reflect(loaded, llm=llm, session=session, catalog=catalog, config=config,
                                 judge_llm=judge_llm)
        except Exception as exc:
            record = {"task_id": run.task_id, "run_dir": run.run_dir.name, "stop_reason": "error",
                      "error": f"{type(exc).__name__}: {exc}", "model": args.model}
            path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{stem}: error before or outside the reflection: {record['error']}")
            continue
        record = to_record(loaded, result)
        record["model"] = args.model
        record["judge_model"] = args.judge_model or args.model
        record["llm_options"] = llm_options
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{stem}: {result.stop_reason}, {len(result.accepted)} accepted, "
              f"{len(result.rejected)} rejected, {len(result.logged)} logged, "
              f"{len(result.judge_errors)} judge errors, "
              f"{result.probes_used} probes -> {path.relative_to(_REPO_ROOT)}")
        if result.stop_reason == "interrupted":
            print("interrupted; the partial record above was saved and the batch stops here")
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
