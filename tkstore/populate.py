"""Algorithm 2 entry point driven by a real agent output directory.

The two pre-existing entry points each drop part of the paper's experience
tuple: `run_diff_for_instance` adapts the inputs correctly but writes a 9-column
store, and `build_knowledge_from_example` writes the store correctly but throws
the execution trace away. This one keeps both halves and leaves the originals
untouched.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from evaluation.evaluate import agent_result_matches_gold
from src.executors.factory import make_executor
from src.utils.agent_utils import infer_engine, load_external_knowledge
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
