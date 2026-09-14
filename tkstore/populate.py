"""Algorithm 2 entry point driven by a real agent output directory.

The two pre-existing entry points each drop part of the paper's experience
tuple: `run_diff_for_instance` adapts the inputs correctly but writes a 9-column
store, and `build_knowledge_from_example` writes the store correctly but throws
the execution trace away. This one keeps both halves and leaves the originals
untouched.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from evaluation.evaluate import agent_result_matches_gold
from src.executors.factory import make_executor
from src.utils.agent_utils import infer_engine, load_external_knowledge
from src.utils.db_paths import (
    _default_repo_root,
    _read_dotenv_value,
    resolve_sqlite_db_path,
)
from src.utils.splits import load_split
from tkboost import TKStore

from .builder import (
    _extract_clean_summary,
    _extract_memories_from_rules,
    _persist_via_tkstore,
)
from .format_utils import _is_csv_like, format_csv_as_table
from .harness import generate_memory_diff_first_turn, generate_rules_from_diff
from .tagger_index import generate_tagged_memories_json


_TIMESTAMP_SUFFIX = re.compile(r"_\d{8}_\d{6}$")


def instance_id_from_output_dir(output_dir: str) -> str:
    """Recover the instance id from the runner's `<id>_YYYYMMDD_HHMMSS` name."""
    name = Path(output_dir).name
    return _TIMESTAMP_SUFFIX.sub("", name)


def _load_instance_record(jsonl_path: str, instance_id: str) -> Dict[str, Any]:
    with open(jsonl_path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            obj = json.loads(line)
            if str(obj.get("instance_id")) == str(instance_id):
                return obj
    raise KeyError(f"{instance_id!r} is absent from {jsonl_path!r}")


def _read_text(path: Path) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _as_markdown(csv_text: Optional[str]) -> Optional[str]:
    """CSV becomes a markdown table; error strings pass through untouched."""
    return format_csv_as_table(csv_text) if _is_csv_like(csv_text) else csv_text


def _last_sql_error_in_messages(messages_path: Path) -> Optional[str]:
    try:
        messages = json.loads(messages_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for message in reversed(messages if isinstance(messages, list) else []):
        content = str(message.get("content") or "")
        if content.startswith("SQL_ERROR:"):
            return content
    return None


def _agent_result_for_diff(
    out: Path, engine: str, db_path_or_cred: Optional[str]
) -> Optional[str]:
    """The agent's result as the model should see it.

    An empty CSV means the final SQL never executed, and the runner only prints
    that error, so it is recovered by re-running the query. Without it the model
    cannot tell a broken query from a genuinely empty result set.
    """
    raw = _read_text(out / "execution_result.csv")
    if raw and raw.strip():
        return _as_markdown(raw)

    agent_sql = _read_text(out / "execution_query.sql") or ""
    if agent_sql.strip() and db_path_or_cred:
        try:
            make_executor(engine, db_path_or_cred).execute(agent_sql)
        except Exception as exc:
            return f"SQL_ERROR: {exc}"

    return _last_sql_error_in_messages(out / "messages.json") or raw


def populate_from_output_dir(
    output_dir: str,
    instance_id: Optional[str] = None,
    engine: Optional[str] = None,
    jsonl_path: Optional[str] = None,
    gold_sql_dir: str = "evaluation/gold/sql",
    gold_dir: str = "evaluation/gold",
    store: Optional[str] = None,
    db_name: Optional[str] = None,
    db_path_or_cred: Optional[str] = None,
    model: Optional[str] = None,
    max_turns: int = 6,
    verbose: bool = True,
) -> Dict[str, Any]:
    out = Path(output_dir)
    if not out.is_dir():
        raise FileNotFoundError(f"output_dir does not exist: {out}")

    instance_id = instance_id or instance_id_from_output_dir(output_dir)
    engine = engine or infer_engine(instance_id)
    record = _load_instance_record(jsonl_path, instance_id)
    db_name = db_name or record.get("db")

    result: Dict[str, Any] = {
        "instance_id": instance_id,
        "engine": engine,
        "store": store,
        "db": db_name,
        "rule_count": 0,
        "skipped": None,
        "inserted": [],
        "diff_text": "",
        "tagged": {},
    }

    # Algorithm 3 line 1 learns from the incorrect SQL only, so a correct run is
    # dropped before any model call.
    if agent_result_matches_gold(str(out / "execution_result.csv"), instance_id, gold_dir):
        result["skipped"] = "correct"
        return result

    gold_sql_path = Path(gold_sql_dir) / f"{instance_id}.sql"
    if not gold_sql_path.exists():
        raise FileNotFoundError(f"Missing gold SQL for {instance_id!r}: {gold_sql_path}")

    external_knowledge = None
    if record.get("external_knowledge"):
        external_knowledge = load_external_knowledge(instance_id, record["external_knowledge"])
    agent_result_text = _agent_result_for_diff(out, engine, db_path_or_cred)

    diff_text = generate_memory_diff_first_turn(
        instance_id=instance_id,
        user_query=record.get("question") or "",
        agent_cte_text=_read_text(out / "execution_query.sql") or "",
        gold_sql_text=gold_sql_path.read_text(encoding="utf-8"),
        gold_result_csv_text=_as_markdown(_read_text(out / "gt_result.csv")) or "",
        agent_full_sql_text=_read_text(out / "execution_query.sql") or "",
        agent_result_csv_text=agent_result_text,
        processed_trace_text=_read_text(out / "processed_trace.txt"),
        engine=engine,
        db_path_or_cred=db_path_or_cred,
        db_name=db_name,
        external_knowledge=external_knowledge,
        max_turns=max_turns,
        model=model,
        verbose=verbose,
    )
    result["diff_text"] = diff_text or ""

    rules_text = generate_rules_from_diff(
        diff_text or "",
        _read_text(out / "execution_query.sql") or "",
        agent_result_text,
        model=model,
        verbose=verbose,
    )
    memories = _extract_memories_from_rules(rules_text or "")

    tagged = generate_tagged_memories_json(
        instance_id=instance_id,
        user_query=record.get("question") or "",
        db_name=db_name,
        gold_sql=gold_sql_path.read_text(encoding="utf-8"),
        agent_sql=_read_text(out / "execution_query.sql") or "",
        clean_summary=_extract_clean_summary(diff_text or ""),
        database_memories=memories["database_memories"],
        generic_memories=memories["generic_memories"],
        evidence=external_knowledge,
        model=model,
        verbose=verbose,
    )
    result["tagged"] = tagged or {}

    tkstore_obj = TKStore(store)
    before = len(tkstore_obj.rows())
    _persist_via_tkstore(
        tkstore_obj,
        tagged or {},
        instance_id,
        db_name,
        # A question-scope row is never retrievable, so the summary is passed to
        # the tagger but not written to the store.
        clean_summary="",
    )
    rows = tkstore_obj.rows()
    result["inserted"] = rows[before:]
    result["rule_count"] = len(rows) - before
    return result


class SplitLeakError(ValueError):
    """Raised when --outputs-base contains an instance that is not in the split."""

    def __init__(self, extra: List[str], split_file: str):
        self.extra = extra
        super().__init__(
            f"outputs-base contains instance ids not in {split_file}: {extra}. "
            "Refusing to populate to avoid train/test leakage."
        )


def _instance_dirs(outputs_base: Path) -> Dict[str, List[Path]]:
    grouped: Dict[str, List[Path]] = {}
    if not outputs_base.is_dir():
        raise FileNotFoundError(f"outputs-base does not exist: {outputs_base}")
    for path in sorted(outputs_base.iterdir()):
        if not path.is_dir():
            continue
        grouped.setdefault(instance_id_from_output_dir(str(path)), []).append(path)
    return grouped


def _completed_dir(dirs: List[Path]) -> Optional[Path]:
    completed = [
        path
        for path in dirs
        if (path / "execution_query.sql").exists()
        and (path / "execution_query.sql").stat().st_size > 0
    ]
    if not completed:
        return None
    return max(completed, key=lambda path: path.stat().st_mtime)


def _default_model() -> Optional[str]:
    return os.environ.get("TKBOOST_MODEL") or _read_dotenv_value(
        _default_repo_root() / ".env", "TKBOOST_MODEL"
    )


def populate_split(
    outputs_base: str,
    split_file: str,
    jsonl_path: str,
    store: str,
    gold_dir: str = "evaluation/gold",
    gold_sql_dir: str = "evaluation/gold/sql",
    model: Optional[str] = None,
    rebuild: bool = True,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Populate a store from every completed output whose id is in the split.

    Directories whose instance id is not in the split abort the run. The store
    is wiped first so a rerun cannot accumulate duplicate rules.
    """
    allowed = load_split(Path(split_file))
    grouped = _instance_dirs(Path(outputs_base))
    extra = sorted(set(grouped) - set(allowed))
    if extra:
        raise SplitLeakError(extra, split_file)

    store_path = Path(store)
    if rebuild and store_path.exists():
        store_path.unlink()

    instances: List[Dict[str, Any]] = []
    for instance_id in allowed:
        completed = _completed_dir(grouped.get(instance_id, []))
        if completed is None:
            instances.append(
                {
                    "instance_id": instance_id,
                    "rule_count": 0,
                    "skipped": "missing_output",
                    "error": None,
                    "output_dir": None,
                }
            )
            continue
        try:
            result = populate_from_output_dir(
                output_dir=str(completed),
                instance_id=instance_id,
                jsonl_path=jsonl_path,
                gold_sql_dir=gold_sql_dir,
                gold_dir=gold_dir,
                store=str(store_path),
                db_path_or_cred=resolve_sqlite_db_path(instance_id),
                model=model,
                verbose=verbose,
            )
            instances.append(
                {
                    "instance_id": instance_id,
                    "rule_count": result.get("rule_count", 0),
                    "skipped": result.get("skipped"),
                    "error": None,
                    "output_dir": str(completed),
                    "db": result.get("db"),
                }
            )
        except Exception as exc:
            instances.append(
                {
                    "instance_id": instance_id,
                    "rule_count": 0,
                    "skipped": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "output_dir": str(completed),
                }
            )

    report = {
        "store": str(store_path),
        "split_file": split_file,
        "outputs_base": str(Path(outputs_base)),
        "instances": instances,
    }
    report_path = Path(outputs_base) / "populate_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Populate a TK-Store from agent outputs on a train split."
    )
    parser.add_argument("--outputs-base", required=True)
    parser.add_argument("--split-file", required=True)
    parser.add_argument("--jsonl-path", default="data/spider2-lite.jsonl")
    parser.add_argument("--store", default="artifacts/tkstore_sqlite.csv")
    parser.add_argument("--gold-dir", default="evaluation/gold")
    parser.add_argument("--gold-sql-dir", default="evaluation/gold/sql")
    parser.add_argument("--model", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Append to an existing store instead of wiping it first.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = populate_split(
            outputs_base=args.outputs_base,
            split_file=args.split_file,
            jsonl_path=args.jsonl_path,
            store=args.store,
            gold_dir=args.gold_dir,
            gold_sql_dir=args.gold_sql_dir,
            model=args.model or _default_model(),
            rebuild=not args.no_rebuild,
            verbose=args.verbose,
        )
    except SplitLeakError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    populated = sum(1 for row in report["instances"] if row["rule_count"] > 0)
    skipped = sum(1 for row in report["instances"] if row["skipped"])
    failed = sum(1 for row in report["instances"] if row["error"])
    print(
        f"populate: {populated} with rules, {skipped} skipped, {failed} failed; "
        f"store={report['store']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
