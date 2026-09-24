"""Helpers for BIRD mini-dev instance ids, splits, and gold eval standards."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Mapping, MutableMapping, Sequence

TRAIN_SEED = 0
TRAIN_FRACTION = 0.25

_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)


def dedupe_exact_rows(records: Sequence[Mapping]) -> List[dict]:
    """Keep the first of each fully identical JSON object, in original order."""
    seen = set()
    kept: List[dict] = []
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        kept.append(dict(record))
    return kept


def load_minidev_records(json_path: Path) -> List[dict]:
    """Read mini_dev_sqlite.json, drop exact duplicate rows, stamp instance ids."""
    json_path = Path(json_path)
    if not json_path.is_file():
        raise FileNotFoundError(
            f"BIRD mini-dev JSON not found: {json_path}. "
            "Create data/minidev as a symlink to the downloaded minidev tree."
        )
    records = json.loads(json_path.read_text(encoding="utf-8"))
    return assign_minidev_ids(dedupe_exact_rows(records))


def assign_minidev_ids(records: Sequence[Mapping]) -> List[dict]:
    """Stamp ``instance_id`` as ``minidev0000`` … following JSON order."""
    assigned: List[dict] = []
    for index, record in enumerate(records):
        row = dict(record)
        row["instance_id"] = f"minidev{index:04d}"
        assigned.append(row)
    return assigned


def has_outermost_order_by(sql: str) -> bool:
    """True iff the result-ordering ``ORDER BY`` is at parenthesis depth 0.

    ``ORDER BY`` inside comments, string literals, subqueries, CTE bodies, and
    window ``OVER (...)`` clauses does not count.
    """
    stripped = _mask_comments_and_strings(sql or "")
    depth = 0
    for match in _ORDER_BY_RE.finditer(stripped):
        prefix = stripped[: match.start()]
        depth = prefix.count("(") - prefix.count(")")
        if depth == 0:
            return True
    return False


def eval_standard_record(instance_id: str, sql: str) -> Dict:
    """One ``spider2lite_eval.jsonl`` row for a BIRD instance."""
    return {
        "instance_id": instance_id,
        "condition_cols": [],
        "ignore_order": not has_outermost_order_by(sql),
    }


def _mask_comments_and_strings(sql: str) -> str:
    """Replace comments and string literals with spaces of the same length."""
    out: List[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(" " * (end - i))
            i = end
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    j += 1
                    break
                if sql[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def index_fields(record: MutableMapping) -> Dict:
    return {
        "instance_id": record["instance_id"],
        "question_id": record["question_id"],
        "db_id": record["db_id"],
        "difficulty": record["difficulty"],
    }


def spider2_shaped_instance(record: Mapping) -> Dict:
    """One populate JSONL row: Spider2 field names, BIRD evidence as inline text."""
    evidence = (record.get("evidence") or "").strip()
    return {
        "instance_id": record["instance_id"],
        "db": record["db_id"],
        "question": record["question"],
        "external_knowledge": evidence or None,
    }


def write_bird_jsonl(path: Path, records: Sequence[Mapping]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(spider2_shaped_instance(record), ensure_ascii=False)
        for record in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
