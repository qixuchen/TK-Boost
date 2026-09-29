#!/usr/bin/env python3
"""Run the DataClaw reflector on development runs (stage B, step 6).

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=_REPO_ROOT / "data/dataclaw_dev/manifest.csv")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "tmp/dataclaw_dev/reflect")
    parser.add_argument("--tasks", default=",".join(PILOT_TASKS),
                        help="comma-separated task_id prefixes (default: the five pilot tasks)")
    parser.add_argument("--model", help="litellm model id; required unless --dry-run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-probes", type=int, default=20)
    parser.add_argument("--max-finals", type=int, default=3)
    parser.add_argument("--probe-timeout", type=int, default=60)
    parser.add_argument("--max-probe-chars", type=int, default=4000)
    args = parser.parse_args()
    if not args.dry_run and not args.model:
        parser.error("--model is required unless --dry-run")
    args.out_dir = args.out_dir.resolve()

    prefixes = tuple(p.strip() for p in args.tasks.split(",") if p.strip())
    runs = [r for r in load_manifest(args.manifest) if r.task_id.startswith(prefixes)]
    catalog = load_catalog(args.data_dir, _REPO_ROOT / "tmp/dataclaw_cache")
    config = ReflectorConfig(max_probes=args.max_probes, max_finals=args.max_finals)
    args.out_dir.mkdir(parents=True, exist_ok=True)

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

        executor = DockerExecutor(args.data_dir)
        with ProbeSession(executor, timeout=args.probe_timeout, max_llm_chars=args.max_probe_chars) as session:
            result = reflect(loaded, llm=litellm_llm(args.model), session=session, catalog=catalog, config=config)
        record = to_record(loaded, result)
        record["model"] = args.model
        path = args.out_dir / f"{stem}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{stem}: {result.stop_reason}, {len(result.accepted)} accepted, "
              f"{len(result.rejected)} rejected, {len(result.logged)} logged, "
              f"{result.probes_used} probes -> {path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
