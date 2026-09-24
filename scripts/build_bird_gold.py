#!/usr/bin/env python3
"""Execute BIRD mini-dev gold SQL and write evaluation/gold_bird/.

Writes, for each of the 498 deduped instances:

- ``sql/<id>.sql``
- ``exec_result/<id>.csv``
- one line in ``spider2lite_eval.jsonl`` (filename required by evaluate.py)

``ignore_order`` is false only when the gold SQL has an outermost ORDER BY.
Any execution failure aborts with the full list; nothing is skipped silently.

    python scripts/build_bird_gold.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.executors.sqlite_executor import SQLiteExecutor  # noqa: E402
from src.utils.agent_utils import write_csv  # noqa: E402
from src.utils.bird import eval_standard_record, load_minidev_records  # noqa: E402
from src.utils.db_paths import resolve_sqlite_db_path  # noqa: E402

BIRD_JSON = _REPO_ROOT / "data" / "minidev" / "MINIDEV" / "mini_dev_sqlite.json"

GOLD_DIR = _REPO_ROOT / "evaluation" / "gold_bird"


def build_gold(
    records: list,
    gold_dir: Path,
    timeout_seconds: float = 1800.0,
    resume: bool = True,
) -> None:
    sql_dir = gold_dir / "sql"
    result_dir = gold_dir / "exec_result"
    sql_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    failures = []
    standards = []
    for record in records:
        instance_id = record["instance_id"]
        db_id = record["db_id"]
        sql = record["SQL"]
        (sql_dir / f"{instance_id}.sql").write_text(sql.rstrip() + "\n", encoding="utf-8")
        standards.append(eval_standard_record(instance_id, sql))

        result_path = result_dir / f"{instance_id}.csv"
        if resume and result_path.is_file():
            continue

        print(f"  executing {instance_id} ({db_id}) ...", flush=True)

        db_path = resolve_sqlite_db_path(instance_id, db_id)
        if not db_path:
            failures.append((instance_id, db_id, "could not resolve sqlite path"))
            continue
        try:
            headers, rows = SQLiteExecutor(
                db_path, timeout_seconds=timeout_seconds
            ).execute(sql)
        except Exception as exc:  # noqa: BLE001 — surface every gold-SQL failure
            failures.append((instance_id, db_id, str(exc)))
            continue
        write_csv(headers, rows, result_path)

    eval_path = gold_dir / "spider2lite_eval.jsonl"
    with eval_path.open("w", encoding="utf-8") as f:
        for row in standards:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if failures:
        lines = "\n".join(f"  {iid} ({db}): {err}" for iid, db, err in failures)
        raise RuntimeError(
            f"{len(failures)} gold SQL statement(s) failed to execute:\n{lines}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bird-json", type=Path, default=BIRD_JSON)
    parser.add_argument("--gold-dir", type=Path, default=GOLD_DIR)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1800.0,
        help="Per-query interrupt timeout. Some mini-dev gold joins exceed the "
        "agent's 120s cap on WSL; gold results still need to be materialised.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-execute every gold SQL even if the CSV already exists.",
    )
    args = parser.parse_args()

    records = load_minidev_records(args.bird_json)
    print(f"executing {len(records)} gold SQL statements into {args.gold_dir}")
    build_gold(
        records,
        args.gold_dir,
        timeout_seconds=args.timeout_seconds,
        resume=not args.no_resume,
    )
    sql_n = len(list((args.gold_dir / "sql").glob("*.sql")))
    csv_n = len(list((args.gold_dir / "exec_result").glob("*.csv")))
    print(f"wrote {sql_n} sql files and {csv_n} exec_result CSVs")
    print(f"wrote {args.gold_dir / 'spider2lite_eval.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
