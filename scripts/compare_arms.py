#!/usr/bin/env python3
"""Build the stage 4 comparison table from two refinement arms.

Run `evaluation/evaluate.py` on each arm directory first, so that both hold an
`evals.csv`. Do *not* evaluate the shared agent directory: `evaluate.py` deletes
duplicate-id directories, and that directory is the input to both arms. The
bare-agent score is taken from the arms' `score` column instead, which also
cross-checks that they refined the same starting query.

    python evaluation/evaluate.py --mode exec_result \
      --result_dir outputs/test_refonly --gold_dir evaluation/gold
    python evaluation/evaluate.py --mode exec_result \
      --result_dir outputs/test_tk --gold_dir evaluation/gold

    python scripts/compare_arms.py \
      --arm-a outputs/test_refonly --arm-b outputs/test_tk \
      --tkstore tkstore/tkstore_sqlite.csv --out outputs/comparison.csv

`delta_knowledge` (arm B - arm A) is attributable to tribal knowledge. `delta_paper`
(arm B - bare) matches the paper's Fig. 6 but also carries the refiner's own gain.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.utils.compare import compare_arms, summarise_by_db_rules  # noqa: E402

COLUMNS = [
    "instance_id", "db", "score_bare", "score_arm_a", "score_arm_b",
    "delta_knowledge", "delta_paper", "rules_used", "n_db_rules", "paired",
]


def load_instance_dbs(jsonl_path: Path) -> dict:
    dbs = {}
    with Path(jsonl_path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            dbs[item["instance_id"]] = item.get("db") or item.get("db_id") or ""
    return dbs


def write_table(rows: list, out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for r in rows:
            writer.writerow([
                r.instance_id, r.db, r.score_bare, r.score_arm_a, r.score_arm_b,
                r.delta_knowledge, r.delta_paper, ";".join(r.rules_used), r.n_db_rules,
                int(r.paired),
            ])


def print_summary(rows: list) -> None:
    groups = summarise_by_db_rules(rows)
    print(f"\n{'group':14s} {'n':>4s} {'bare':>10s} {'arm A':>10s} {'arm B':>10s} {'B-A':>6s} {'+1':>4s} {'-1':>4s}")
    print("-" * 72)
    for g in groups:
        pct = lambda k: f"{k:>3d} ({k / g.n:.0%})" if g.n else "  0"
        print(f"{g.label:14s} {g.n:>4d} {pct(g.bare):>10s} {pct(g.arm_a):>10s} {pct(g.arm_b):>10s} "
              f"{g.arm_b - g.arm_a:>+6d} {g.improved:>4d} {g.regressed:>4d}")
    print("\n`bare` is the agent with no refiner and no knowledge; `arm A` adds the refiner;")
    print("`arm B` adds knowledge on top. B-A is the knowledge gain, +1 / -1 count the")
    print("instances knowledge fixed and broke -- a net zero can hide one of each.")
    unpaired = groups[0].unpaired
    if unpaired:
        print(f"\n⚠️  {unpaired} instance(s) excluded from every group above: the arms disagree on")
        print("   their bare score, so those deltas are not attributable to knowledge.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arm-a", required=True, help="Refiner without a store (holds evals.csv)")
    p.add_argument("--arm-b", required=True, help="Refiner with a store (holds evals.csv)")
    p.add_argument("--tkstore", required=True, help="The store arm B used, for n_db_rules")
    p.add_argument("--jsonl-path", default="data/spider2-lite.jsonl", help="JSONL with instance databases")
    p.add_argument("--out", default=None, help="Write the per-instance table here as CSV")
    args = p.parse_args()

    try:
        result = compare_arms(args.arm_a, args.arm_b, args.tkstore, load_instance_dbs(args.jsonl_path))
    except FileNotFoundError as e:
        p.error(str(e))

    if not result.rows:
        print("❌ No instances are scored in both arms. Run evaluate.py on each arm first.")
        return 1

    for warning in result.warnings:
        print(f"⚠️  {warning}")

    print_summary(result.rows)

    changed = [r for r in result.rows if r.delta_knowledge]
    if changed:
        print(f"\nInstances knowledge changed ({len(changed)}):")
        for r in changed:
            print(f"  {r.instance_id:10s} {r.db:24s} B-A={r.delta_knowledge:+d} "
                  f"n_db_rules={r.n_db_rules:<3d} rules_used={';'.join(r.rules_used) or '-'}")

    if args.out:
        write_table(result.rows, args.out)
        print(f"\n📄 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
