#!/usr/bin/env python3
"""Run the three-arm comparison of stage 4.

One agent run per batch, then two refinement passes over that same output:

    agent        bare agent, no refiner, no knowledge  -> the `score` column
    arm_refonly  refiner without a store               -> `score_final`
    arm_tk       refiner with a store                  -> `score_final`

Knowledge gain is `arm_tk - arm_refonly`, which is a paired comparison because both
arms refine byte-identical starting SQL. The paper's Fig. 6 reading is
`arm_tk - agent`, which also carries the refiner's own gain.

Evaluation is deliberately *not* run from here; score the directories with
`evaluation/evaluate.py` once you have decided to.

    # see the commands without running anything
    python scripts/run_pipeline.py --dry-run \
      --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
      --out-prefix outputs/test --tkstore tkstore/tkstore_sqlite.csv

    # a 3-instance rehearsal
    python scripts/run_pipeline.py --split <split> --out-prefix outputs/rehearsal \
      --tkstore tkstore/tkstore_sqlite.csv --split-limit 3

Interrupting is safe: both runner paths resume per instance, so rerunning the same
command picks up where it stopped.
"""

import argparse
import dataclasses
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.utils.pipeline import (  # noqa: E402
    plan_batches,
    plan_steps,
    run_steps,
    verify_shared_agent_output,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", required=True, help="Split file listing the instances to run")
    p.add_argument("--out-prefix", required=True,
                   help="Directories are <prefix>_agent / _refonly / _tk, plus _b<offset> when batched")
    p.add_argument("--tkstore", required=True, help="TK-Store CSV for the knowledge arm")
    p.add_argument("--batch-size", type=int, default=None,
                   help="Split the run into batches of this many instances")
    p.add_argument("--split-limit", type=int, default=None,
                   help="Run only the first N instances of the split, for a rehearsal")
    p.add_argument("--model", type=str, default=None, help="Model for the runner")
    p.add_argument("--filter-model", type=str, default=None, help="Model for the FilterKnowledge step")
    p.add_argument("--no-llm-filtering", action="store_true",
                   help="Skip the FilterKnowledge step of retrieval (ablation only)")
    p.add_argument("--adopt-refiner-sql", action="store_true",
                   help="Substitute the refiner's own suggested_fix_sql in both arms instead of "
                        "asking the agent to rewrite. Reproduces upstream tkboost.sql(), which "
                        "differs from the paper's feedback-then-agent-revises loop")
    p.add_argument("--validate-fix-in-context", action="store_true",
                   help="Show the refiner what reads its output and judge its suggested_fix_sql "
                        "by the reassembled query rather than the fragment alone, in both arms. "
                        "Requires --adopt-refiner-sql")
    p.add_argument("--verdict-attempts", type=int, default=None,
                   help="How many verdicts the refiner may produce per fragment in both arms. "
                        "Above 1, a suggestion that fails once substituted is handed back with "
                        "the error and asked again. Requires --validate-fix-in-context")
    p.add_argument("--include-candidate-sql", action="store_true",
                   help="Pass the refiner's suggested_fix_sql to the agent as part of the "
                        "feedback in both arms. Keeps the agent as integrator (unlike "
                        "--adopt-refiner-sql) while letting the knowledge-informed SQL through")
    p.add_argument("--refiner-turns", type=int, default=None,
                   help="Probing turns per fragment (omit for the runner default of 25; "
                        "upstream tkboost.sql effectively uses 5)")
    p.add_argument("--refiner-min-probes", type=int, default=None,
                   help="Probes required before the refiner's verdict is accepted "
                        "(omit for 8; upstream tkboost.sql passes 3). Must be lower than "
                        "--refiner-turns or no verdict is reachable")
    p.add_argument("--dry-run", action="store_true", help="Print the commands without running them")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.batch_size is not None and args.split_limit is not None:
        parser.error("--batch-size and --split-limit are mutually exclusive")
    if not Path(args.tkstore).is_file():
        parser.error(f"--tkstore path does not exist: {args.tkstore}")

    # Validated here rather than left to the child: an unreachable probe minimum would
    # otherwise only surface after the agent step had already run for hours. The runner
    # owns the rule so the defaults cannot drift apart.
    try:
        from src.agents.sql_agent_runner import DEFAULT_REFINER_TURNS, _refiner_options
        _refiner_options(argparse.Namespace(
            refiner_turns=args.refiner_turns if args.refiner_turns is not None else DEFAULT_REFINER_TURNS,
            refiner_min_probes=args.refiner_min_probes,
            adopt_refiner_sql=args.adopt_refiner_sql,
            include_candidate_sql=args.include_candidate_sql,
            validate_fix_in_context=args.validate_fix_in_context,
            verdict_attempts=1 if args.verdict_attempts is None else args.verdict_attempts,
        ))
    except ValueError as e:
        parser.error(str(e))

    try:
        batches = plan_batches(args.split, Path(args.out_prefix), batch_size=args.batch_size)
    except (ValueError, FileNotFoundError) as e:
        parser.error(str(e))
    if args.split_limit is not None:
        # A rehearsal is one short batch, not a tiling of the split.
        batches = [dataclasses.replace(batches[0], limit=args.split_limit)]

    print(f"📋 {len(batches)} batch(es) from {args.split}")

    for i, batch in enumerate(batches, start=1):
        print(f"\n{'='*80}\n📦 Batch {i}/{len(batches)}  offset={batch.offset} limit={batch.limit}\n{'='*80}")
        steps = plan_steps(
            batch,
            args.split,
            tkstore=args.tkstore,
            model=args.model,
            filter_model=args.filter_model,
            use_llm_filtering=not args.no_llm_filtering,
            refiner_turns=args.refiner_turns,
            refiner_min_probes=args.refiner_min_probes,
            adopt_refiner_sql=args.adopt_refiner_sql,
            include_candidate_sql=args.include_candidate_sql,
            validate_fix_in_context=args.validate_fix_in_context,
            verdict_attempts=args.verdict_attempts,
        )
        code = run_steps(steps, dry_run=args.dry_run)
        if code != 0:
            print(f"❌ Batch {i} failed; rerun the same command to resume")
            return code
        if args.dry_run:
            continue

        for arm_dir in (batch.refonly_dir, batch.tk_dir):
            mismatched = verify_shared_agent_output(batch.agent_dir, arm_dir)
            if mismatched:
                print(f"❌ {arm_dir.name} does not share the agent's starting SQL for "
                      f"{len(mismatched)} instance(s): {mismatched[:5]}")
                print("   The knowledge comparison would not be paired. Stopping.")
                return 1
            print(f"✅ {arm_dir.name} shares the agent's starting SQL")

    print("\n✅ All batches complete. Score them with evaluation/evaluate.py when ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
