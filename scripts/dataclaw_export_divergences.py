#!/usr/bin/env python3
"""Export accepted reflector divergences as the stage C input file.

Scans every reflector record (*.json) under --reflect-dir and writes one JSON
line per accepted divergence to --out.

    python scripts/dataclaw_export_divergences.py
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tkstore.dataclaw.divergence_store import export_accepted, write_divergences  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reflect-dir", type=Path, default=_REPO_ROOT / "tmp/dataclaw_dev/reflect")
    parser.add_argument("--out", type=Path, default=_REPO_ROOT / "data/dataclaw_dev/divergences.jsonl")
    args = parser.parse_args()

    paths = sorted(p for p in args.reflect_dir.resolve().rglob("*.json"))
    records = export_accepted(paths, root=_REPO_ROOT)
    write_divergences(records, args.out)
    print(f"{len(paths)} records scanned, {len(records)} accepted divergences -> {args.out}")
    for task_id, n in sorted(Counter(r["task_id"] for r in records).items()):
        print(f"  {task_id}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
