#!/usr/bin/env python3
"""Smoke-check SQLite path resolution against the local Spider2 data.

Depends on machine-local data, so it lives here rather than in `tests/`.
`SPIDER2_DB_ROOT` may come from the environment or from the repository's `.env`.

    python scripts/check_db_paths.py
"""

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.utils.db_paths import (  # noqa: E402
    DbPathNotFound,
    _discover_db_root,
    get_database_path,
)


def main() -> int:
    db_root = _discover_db_root(_REPO_ROOT)
    if db_root is None:
        print(
            "SPIDER2_DB_ROOT is set neither in the environment nor in .env; "
            "set it before running this check."
        )
        return 2
    print(f"database root: {db_root}")

    map_path = _REPO_ROOT / "data" / "spider2_local_map.json"
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    print(f"Checking {len(mapping)} instances from {map_path.name} ...")

    failures = []
    missing_files = []
    resolved_dbs = set()
    for instance_id, db_id in sorted(mapping.items()):
        try:
            path = get_database_path(instance_id, db_id)
        except DbPathNotFound as exc:
            failures.append((instance_id, exc))
            continue
        if not Path(path).is_file():
            missing_files.append((instance_id, path))
        resolved_dbs.add(Path(path).name)

    print(f"resolved: {len(mapping) - len(failures)}/{len(mapping)}")
    print(f"unique databases: {len(resolved_dbs)}")

    for instance_id, exc in failures:
        print(f"\nFAILED {instance_id}\n{exc}")
    for instance_id, path in missing_files:
        print(f"\nRESOLVED BUT MISSING {instance_id}: {path}")

    return 1 if (failures or missing_files) else 0


if __name__ == "__main__":
    raise SystemExit(main())
