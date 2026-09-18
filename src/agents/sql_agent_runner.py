#!/usr/bin/env python3
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd

import litellm

from src.executors import Executor
from src.executors.factory import make_executor
from src.utils.agent_utils import (
    format_table,
    detect_sql_blocks,
    detect_solution,
    ensure_dir,
    write_csv,
    load_predicted_cte_briefs,
    load_predicted_tables_columns,
    infer_engine,
    parse_ctes_from_sql,
    rebuild_sql_from_ctes,
    extract_goal_from_cte_body,
    make_json_serializable,
    load_external_knowledge,
)
from src.agents.cte_refiner import run_refiner as refiner_run, DEFAULT_MIN_PROBES

# Probing turns each fragment gets. 25 is what this entry point has always used and what
# the 86-instance reference run was measured with; upstream `tkboost.sql()` effectively
# uses 5. Both are reproducible via --refiner-turns, so the default must not drift.
DEFAULT_REFINER_TURNS = 25
from src.agents.prompts import BASE_PROMPT, SNOWFLAKE_PROMPT
from src.utils.db_paths import resolve_sqlite_db_path
from src.utils.splits import load_split
from src.utils.auth import configure_llm_env, USE_OPENAI
from tkstore.tagger_index import search_index_for_sql, _llm_filter_relevant_rules


# ----------------- LLM Provider Mapping -----------------
AZURE_TO_OPENAI_MODEL = {
    "azure/gpt-4.1": "gpt-4.1",
    "azure/gpt-4o": "gpt-4o",
    "azure/o4-mini": "o4-mini",
}


def _is_openai_provider() -> bool:
    """True when using OpenAI directly (not Azure)."""
    if os.environ.get("OPENAI_API_KEY"):
        return True
    if os.environ.get("AZURE_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY"):
        return False
    return USE_OPENAI


def _resolve_model(model: str) -> str:
    return AZURE_TO_OPENAI_MODEL.get(model, model) if _is_openai_provider() else model


def llm_completion(model: str, messages: list, **params):
    return litellm.completion(model=_resolve_model(model), messages=messages, **params)


# ----------------- Tribal Knowledge Retrieval (Alg 4) -----------------
RULE_SCOPES = ('all', 'db', 'generic')


def _keep_scope(candidates: List[dict], rule_scope: str) -> List[dict]:
    """Restrict candidates to one scope.

    Done here rather than in `tkstore/tagger_index.py` to leave the vendored upstream
    retrieval untouched, and before FilterKnowledge so no LLM call is spent ranking
    rules that are already excluded.
    """
    if rule_scope == 'all':
        return candidates
    wanted_generic = (rule_scope == 'generic')
    return [c for c in candidates
            if ((c.get('scope') or '').strip().lower() == 'generic') == wanted_generic]


def _retrieve_rules_for(sql_text: str,
                        tkstore_path: str,
                        db: Optional[str],
                        use_llm_filtering: bool,
                        filter_model: str,
                        rule_scope: str = 'all') -> Tuple[List[dict], List[dict]]:
    """Retrieve rules for one SQL fragment, as (candidates, selected).

    `MemoryRetriever.retrieve` collapses the two stages into one return value; they
    are kept apart here so the report can show what FilterKnowledge dropped.

    `tkstore.tagger_index` has no provider mapping of its own and swallows LLM
    errors by returning every candidate, so an unmapped 'azure/...' name would
    silently disable filtering rather than fail.
    """
    candidates = _keep_scope(
        search_index_for_sql(
            sql_text,
            tkstore_path,
            generic_only=False,
            db=db,
        ),
        rule_scope,
    )
    if use_llm_filtering and candidates:
        resolved = _resolve_model(filter_model)
        if _is_openai_provider() and resolved.startswith('azure/'):
            raise ValueError(
                f"filter_model {filter_model!r} has no OpenAI equivalent in "
                f"AZURE_TO_OPENAI_MODEL; it would disable FilterKnowledge silently"
            )
        selected = _llm_filter_relevant_rules(sql_text, candidates, db=db, model=resolved)
        return candidates, selected
    return candidates, candidates


def _knowledge_block(rules: List[dict]) -> str:
    lines = []
    for r in rules[:40]:
        txt = (r.get('rule') or '').strip()
        if txt:
            lines.append(f"- {txt}")
    return "\n".join(lines)


def _goal_with_knowledge(goal: str, rules: List[dict]) -> str:
    block = _knowledge_block(rules)
    if not block:
        return goal
    return f"{goal}\n\nUse these tribal knowledge rules as guidance:\n\n{block}"


def _revise_from_feedback(
    *,
    feedback: str,
    artifact_suffix: str,
    messages: List[dict],
    model: str,
    engine: str,
    db_path_or_cred,
    out_dir: Path,
) -> Optional[str]:
    """Feed a refiner verdict back to the agent and adopt its first executable rewrite.

    Returns the adopted SQL, or None when no attempt produced a runnable solution.
    """
    messages.append({"role": "user", "content": feedback})
    for _attempt in range(1, 6):
        resp = llm_completion(model=model, messages=messages)
        msg = resp['choices'][0]['message']
        content = (msg.get('content') or msg.get('reasoning_content') or "")
        if not content:
            continue
        messages.append({"role": "assistant", "content": content})
        # Execute any <sql> probes returned during cooperation
        sql_blocks = detect_sql_blocks(content)
        if sql_blocks:
            try:
                temp_executor = make_executor(engine, db_path_or_cred)
                headers_ref, rows_ref = temp_executor.execute(sql_blocks[0].strip())
                messages.append({"role": "user",
                                 "content": "SQL_RESULT_TABLE:\n" + format_table(headers_ref, rows_ref)})
                if hasattr(temp_executor, 'close'):
                    temp_executor.close()
            except Exception as e_sql:
                messages.append({"role": "user", "content": f"SQL_ERROR: {e_sql}"})
        new_sol = detect_solution(content)
        if not new_sol:
            continue
        try:
            temp_executor = make_executor(engine, db_path_or_cred)
            headers_new, rows_new = temp_executor.execute(new_sol)
            if hasattr(temp_executor, 'close'):
                temp_executor.close()
        except Exception as e_exec:
            messages.append({"role": "user", "content": f"SQL_ERROR: {e_exec}"})
            continue
        (out_dir / f"execution_query_after_{artifact_suffix}.sql").write_text(new_sol, encoding='utf-8')
        write_csv(headers_new, rows_new, out_dir / f"execution_result_after_{artifact_suffix}.csv")
        return new_sol
    return None


def _feedback_text(verdict: dict, target: str, instruction: str) -> Optional[str]:
    """Render a refiner verdict as agent-facing feedback, or None when it found no issues."""
    if str((verdict or {}).get('status', '')).lower() not in ('issues', 'issue', 'incorrect'):
        return None
    issues = verdict.get('issues') if isinstance((verdict or {}).get('issues'), list) else []
    suggested = verdict.get('suggested_fix') or ''
    tests = verdict.get('tests') if isinstance(verdict.get('tests'), list) else []
    lines = [f"[Refiner feedback for {target}]", "Issues:"]
    lines.extend([f"- {it}" for it in issues[:10]] or ["- <none>"])
    if suggested:
        lines.append("\nSuggested fix (reference):\n" + suggested)
    if tests:
        lines.append("\nTests / checks to satisfy:")
        lines.extend([f"- {t}" for t in tests[:5]])
    lines.append(instruction)
    return "\n".join(lines)


# ----------------- Final Artifact Selection -----------------
def _choose_and_mark_final_artifacts(output_dir: Path, last_cte_name: str = None):
    """Pick the final SQL/CSV artifacts based on precedence and copy to *_final.* files.
    Precedence (highest first):
      - execution_query_after_<last_cte_name>.sql / execution_result_after_<last_cte_name>.csv (when provided and exist)
      - latest execution_query_after_*.sql / execution_result_after_*.csv by mtime
      - execution_query.sql / execution_result.csv (original)
    
    Note: _refined files are no longer used - if refiner made changes, they're saved as _after_ files.
    """
    try:
        output_dir = Path(output_dir)
        chosen_sql = None
        chosen_csv = None
        # The final SELECT is refined after every CTE, so its revision supersedes them
        fs_sql = output_dir / "execution_query_after_final_select.sql"
        fs_csv = output_dir / "execution_result_after_final_select.csv"
        if fs_sql.exists():
            chosen_sql = fs_sql
        if fs_csv.exists():
            chosen_csv = fs_csv
        # Prefer after files for the last CTE if provided
        if last_cte_name:
            sql_path = output_dir / f"execution_query_after_{last_cte_name}.sql"
            csv_path = output_dir / f"execution_result_after_{last_cte_name}.csv"
            if chosen_sql is None and sql_path.exists():
                chosen_sql = sql_path
            if chosen_csv is None and csv_path.exists():
                chosen_csv = csv_path
        # Latest revised artifacts (from refiner revisions)
        if chosen_sql is None:
            after_sqls = sorted(output_dir.glob('execution_query_after_*.sql'), key=lambda p: p.stat().st_mtime)
            if after_sqls:
                chosen_sql = after_sqls[-1]
        if chosen_csv is None:
            after_csvs = sorted(output_dir.glob('execution_result_after_*.csv'), key=lambda p: p.stat().st_mtime)
            if after_csvs:
                chosen_csv = after_csvs[-1]
        # Original artifacts (fallback if no refiner changes)
        if chosen_sql is None:
            osql = output_dir / 'execution_query.sql'
            if osql.exists():
                chosen_sql = osql
        if chosen_csv is None:
            ocsv = output_dir / 'execution_result.csv'
            if ocsv.exists():
                chosen_csv = ocsv
        # Copy to *_final.* if any selected
        final_sql_path = output_dir / 'execution_query_final.sql'
        final_csv_path = output_dir / 'execution_result_final.csv'
        if chosen_sql and chosen_sql.exists():
            shutil.copyfile(str(chosen_sql), str(final_sql_path))
        if chosen_csv and chosen_csv.exists():
            shutil.copyfile(str(chosen_csv), str(final_csv_path))
    except Exception as e:
        print(f"⚠️  Could not choose final artifacts: {e}")


# ----------------- Data Model -----------------
@dataclass
class Instance:
    instance_id: str
    db: str
    question: str
    external_knowledge: Optional[str] = None


# ----------------- Trace Generation -----------------
def generate_processed_trace(messages: List[dict]) -> str:
    """Generate a human-readable trace from messages list."""
    trace_lines = [
        "PREVIOUS TRACE (reference only, do NOT repeat):",
        "=" * 60
    ]
    
    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content", "").strip()
        
        if not content:
            continue
        
        if i > 0:
            trace_lines.append("")
        
        # Process user messages
        if role == "user":
            if 'SQL_RESULT' in content:
                trace_lines.extend([
                    "── SQL RESULT ──────────────────────────────────────────────",
                    f"{content}",
                    "============================================================"
                ])
            elif 'SQL_ERROR' in content:
                trace_lines.extend([
                    "── SQL ERROR ───────────────────────────────────────────────",
                    f"{content}",
                    "============================================================"
                ])
            else:
                trace_lines.extend([
                    "── USER QUESTION ─────────────────────────────────────────",
                    content,
                    "============================================================"
                ])
        
        # Process assistant messages
        elif role == "assistant":
            if "<sql>" in content:
                sql_text = content.replace("<sql>", "").replace("</sql>", "").strip()
                trace_lines.extend([
                    "── ASSISTANT SQL QUERY ───────────────────────────────────",
                    f"{sql_text}",
                    "──────────────────────────────────────────────────────────"
                ])
            elif "<solution>" in content:
                sol_text = content.replace("<solution>", "").replace("</solution>", "").strip()
                trace_lines.extend([
                    "── ASSISTANT SOLUTION ────────────────────────────────────",
                    f"{sol_text}",
                    "──────────────────────────────────────────────────────────"
                ])
            elif "<thinking>" in content or "<think>" in content:
                think_text = content.replace("<thinking>", "").replace("</thinking>", "").replace("<think>", "").replace("</think>", "").strip()
                trace_lines.extend([
                    "── ASSISTANT THINKING ────────────────────────────────────",
                    f"{think_text}",
                    "──────────────────────────────────────────────────────────"
                ])
            else:
                trace_lines.extend([
                    "── ASSISTANT RESPONSE ────────────────────────────────────",
                    f"{content}",
                    "──────────────────────────────────────────────────────────"
                ])
        
        elif role == "system":
            trace_lines.extend([
                "── SYSTEM PROMPT ─────────────────────────────────────────",
                content[:500] + "..." if len(content) > 500 else content,
                "──────────────────────────────────────────────────────────"
            ])

    return "\n".join(trace_lines)


# ----------------- Ground Truth Loading -----------------
def load_ground_truth(instance_id: str) -> Tuple[Optional[str], Optional[List[Tuple]], Optional[List[List[str]]]]:
    """Load ground truth SQL query, result, and column names for the given instance_id.
    
    Returns:
        gt_query: Ground truth SQL query (if available)
        gt_result: Ground truth result rows from first valid CSV
        all_col_names: List of column name lists for all valid GT variants (e.g., _a, _b, etc.)
    """
    # Try to load SQL query
    gt_query = None
    sql_file_path = Path(f"evaluation/gold/sql/{instance_id}.sql")
    if sql_file_path.exists():
        gt_query = sql_file_path.read_text().strip()
    
    # Try to load result from CSV - check all possible variants
    gt_result = None
    all_col_names = []
    base_csv_path = Path(f"evaluation/gold/exec_result/{instance_id}.csv")
    
    # Try base path first, then _a, _b, _c, etc. suffixes
    gt_csv_candidates = [base_csv_path]
    for suffix in ['_a', '_b', '_c', '_d', '_e']:
        gt_csv_candidates.append(base_csv_path.with_stem(f"{instance_id}{suffix}"))
    
    for csv_path in gt_csv_candidates:
        if csv_path.exists():
            try:
                df_gt = pd.read_csv(csv_path)
                col_names = list(df_gt.columns)
                all_col_names.append(col_names)
                # Use first valid CSV for gt_result
                if gt_result is None:
                    gt_result = [tuple(row) for row in df_gt.itertuples(index=False, name=None)]
            except Exception as e:
                print(f"⚠️  Warning: Could not load GT result from {csv_path}: {e}")
    
    return gt_query, gt_result, all_col_names


# Predicted loader functions, formatting helpers etc. moved to src/utils/agent_utils


# ----------------- Agent Core -----------------
def get_system_prompt(instance_id: str, train_context_file: str = None) -> str:
    """Return appropriate system prompt based on instance type.
    
    If train_context_file is provided (TEMP EXPERIMENT), prepend its contents
    to the system prompt."""
    if instance_id.lower().startswith('sf'):
        base_prompt = SNOWFLAKE_PROMPT
    else:
        base_prompt = BASE_PROMPT
    
    # TEMP EXPERIMENT: Prepend train context if provided
    if train_context_file:
        try:
            train_context = Path(train_context_file).read_text()
            return train_context + "\n\n" + base_prompt
        except Exception as e:
            print(f"⚠️  Failed to load train context file: {e}")
    
    return base_prompt


def load_snowflake_schema_context(db_name: str) -> Optional[str]:
    """Load compressed schema context for Snowflake database."""
    schema_dir = Path(__file__).resolve().parent.parent.parent / "data" / "sf_schemas"
    schema_file = schema_dir / f"{db_name}.txt"
    if schema_file.exists():
        return schema_file.read_text(encoding='utf-8')
    return None


def build_user_message(inst: Instance, 
                       predicted_cte_hint: Optional[str], 
                       predicted_schema_hint: Optional[str],
                       schema_context: Optional[str] = None,
                       external_knowledge: Optional[str] = None,
                       expected_output_format: Optional[str] = None) -> str:
    msg = ["[USER_QUESTION]", inst.question.strip()]
    if external_knowledge:
        msg += ["", "[EXTERNAL_KNOWLEDGE]", external_knowledge.strip()]
    if schema_context:
        msg += ["", "[SCHEMA_CONTEXT]", schema_context.strip()]
    if predicted_cte_hint:
        msg += ["", "[PREDICTED_CTES_HINT]", predicted_cte_hint.strip()]
    if predicted_schema_hint:
        msg += ["", "[PREDICTED_SCHEMA_HINT]", predicted_schema_hint.strip()]
    if expected_output_format:
        msg += ["", expected_output_format.strip()]
    return "\n".join(msg)


# make_executor now imported from src.executors.factory


def run_agent(inst: Instance,
              engine: str,
              db_path_or_cred: Optional[str],
              model: str,
              predicted_cte_hint: Optional[str],
              predicted_schema_hint: Optional[str],
              schema_context: Optional[str] = None,
              external_knowledge: Optional[str] = None,
              expected_output_format: Optional[str] = None,
              max_turns: int = 25,
              train_context_file: str = None,  # TEMP EXPERIMENT
              verbose: bool = True) -> Tuple[str, Optional[List[str]], List[Tuple], List[dict], Executor]:
    executor = make_executor(engine, db_path_or_cred)
    system_prompt = get_system_prompt(inst.instance_id, train_context_file)  # TEMP EXPERIMENT: pass train_context_file
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_user_message(inst, predicted_cte_hint, predicted_schema_hint, schema_context, external_knowledge, expected_output_format)},
    ]
    final_sql = None
    sql_text = ""

    for turn in range(1, max_turns + 1):
        if verbose:
            print(f"\n{'='*60}")
            print(f"TURN {turn}/{max_turns}")
            print(f"{'='*60}")
        
        resp = llm_completion(model=model, messages=messages)
        msg_obj = resp["choices"][0]["message"]
        content = (msg_obj.get("content") or "").strip()
        reasoning_content = (msg_obj.get("reasoning_content") or "").strip()
        
        # Fallbacks if empty
        if not content:
            content = reasoning_content
        if not reasoning_content:
            reasoning_content = content
        
        # Add assistant response to messages (matching vanilla runner logic)
        if content == reasoning_content:
            messages.append({"role": "assistant", "content": content})
        else:
            # Separate reasoning and content like vanilla runner
            messages.append({"role": "assistant", "content": "<think>" + reasoning_content + "</think>"})
            messages.append({"role": "assistant", "content": content})
        
        if verbose:
            print(f"\n[ASSISTANT RESPONSE]:")
            print(content[:500] + "..." if len(content) > 500 else content)

        sol = detect_solution(content)
        if sol:
            final_sql = sol
            if verbose:
                print(f"\n✅ SOLUTION DETECTED!")
                print(f"[FINAL SQL]:\n{final_sql[:300]}..." if len(final_sql) > 300 else final_sql)
            break

        sql_blocks = detect_sql_blocks(content)
        if not sql_blocks:
            if verbose:
                print(f"\n⚠️  No SQL block detected, prompting agent...")
            messages.append({"role": "user", "content": "Send one <sql> now."})
            continue

        sql_text = sql_blocks[0].strip()
        if verbose:
            print(f"\n[EXECUTING SQL]:")
            print(sql_text[:300] + "..." if len(sql_text) > 300 else sql_text)
        
        try:
            headers, rows = executor.execute(sql_text)
            table_text = format_table(headers, rows)
            if verbose:
                preview = "\n".join(table_text.split("\n")[:10])
                print(f"\n[SQL RESULT] ({len(rows)} rows):")
                print(preview)
                if len(table_text.split("\n")) > 10:
                    print("... (truncated)")
            messages.append({"role": "user", "content": "SQL_RESULT_TABLE:\n" + table_text})
        except Exception as e:
            if verbose:
                print(f"\n❌ [SQL ERROR]: {str(e)}")
            messages.append({"role": "user", "content": f"SQL_ERROR: {str(e)}"})
            continue

    if not final_sql:
        final_sql = sql_text

    # Execute final SQL for output
    headers, rows = (None, [])
    if final_sql:
        try:
            headers, rows = executor.execute(final_sql)
        except Exception as e:
            if verbose:
                print(f"⚠️  Final SQL execution failed: {e}")

    return final_sql, headers, rows, messages, executor


# ----------------- Refiner Integration (optional) -----------------
def run_cte_refiner(instance_id: str,
                      db_id: str,
                      user_query: str,
                      final_sql: str,
                      predicted_ctes_plan: Optional[str],
                      model: str = "azure/gpt-4.1",
                      verbose: bool = True) -> Optional[dict]:
    try:
        return refiner_run(
            instance_id,
            db_id,
            user_query,
            final_sql or "",
            cte_goal="Refine final SELECT",
            predicted_ctes=predicted_ctes_plan or None,
            previous_ctes=None,
            model=model,
            max_turns=25,
            verbose=verbose,
            trace_output_path=None,
            use_all_rules=False,
        )
    except Exception as e:
        if verbose:
            print(f"⚠️  Refiner failed: {e}")
        return None


def perform_refinement_and_revision(inst: Instance,
                                    final_sql: str,
                                    predicted_cte_hint: Optional[str],
                                    engine: str,
                                    db_path_or_cred: Optional[str],
                                    messages: List[dict],
                                    out_dir: Path,
                                    model: str,
                                    verbose: bool,
                                    tribalknowledge_generic_only: bool = True,
                                    external_knowledge: str = None,
                                    schema_context: str = None,
                                    tkstore_path: Optional[str] = None,
                                    use_llm_filtering: bool = True,
                                    filter_model: str = 'gpt-4.1',
                                    rule_scope: str = 'all',
                                    refiner_turns: int = DEFAULT_REFINER_TURNS,
                                    refiner_min_probes: Optional[int] = None,
                                    ) -> Tuple[str, Optional[dict]]:
    """Run per-CTE refiner flow with cooperative revision and final SELECT refinement.

    Returns updated_final_sql, final_select_verdict (optional).
    """
    # Parse CTEs and remainder
    ctes, remainder_sql = parse_ctes_from_sql(final_sql)
    refiner_model = 'azure/gpt-4.1'
    retrievals: List[dict] = []

    def retrieve_for(stage: str, name: str, sql_text: str) -> List[dict]:
        if not tkstore_path:
            return []
        candidates, selected = _retrieve_rules_for(
            sql_text=sql_text,
            tkstore_path=tkstore_path,
            db=inst.db,
            use_llm_filtering=use_llm_filtering,
            filter_model=filter_model,
            rule_scope=rule_scope,
        )
        retrievals.append({
            'stage': stage,
            'name': name,
            'sql_sha1': hashlib.sha1(sql_text.encode('utf-8')).hexdigest(),
            'candidates': [str(r.get('mem_id')) for r in candidates],
            'selected': [str(r.get('mem_id')) for r in selected],
        })
        return selected

    # Indexed rather than iterated: adopting a revision below rebinds `ctes`, and the
    # remaining CTEs must come from that revision.
    idx_cte = 0
    while idx_cte < len(ctes):
        c = ctes[idx_cte]
        cte_name = c.get('name') or ''
        cte_body = c.get('body') or ''
        goal = extract_goal_from_cte_body(cte_body, cte_name)
        with_sql = f"WITH {cte_name} AS (\n{cte_body}\n)"
        prev_blocks = []
        for pc in ctes[:idx_cte]:
            pname = pc.get('name') or ''
            pbody = pc.get('body') or ''
            pgoal = extract_goal_from_cte_body(pbody, pname)
            prev_blocks.append(f"-- CTE: {pname}\n-- Goal: {pgoal}\nWITH {pname} AS (\n{pbody}\n)\n")
        previous_ctes_text = "\n\n".join(prev_blocks).strip()

        cte_out = out_dir / f"refiner_{cte_name}.json"
        cte_trace = out_dir / f"refiner_{cte_name}_trace.txt"
        verdict = refiner_run(
            instance_id=inst.instance_id,
            db_id=inst.db,
            user_query=inst.question,
            cte_text=with_sql,
            cte_goal=_goal_with_knowledge(goal, retrieve_for('cte', cte_name, with_sql)),
            previous_ctes=previous_ctes_text,
            predicted_ctes=predicted_cte_hint or None,
            model=refiner_model,
            max_turns=refiner_turns,
            min_required_sql=refiner_min_probes,
            verbose=verbose,
            trace_output_path=str(cte_trace),
            use_all_rules=False,
            tribalknowledge_generic_only=tribalknowledge_generic_only,
            external_knowledge=external_knowledge,
            schema_context=schema_context,
        )
        cte_out.write_text(json.dumps(verdict, indent=2), encoding='utf-8')

        feedback = _feedback_text(
            verdict,
            f"CTE {cte_name}",
            f"\nInstruction: Revise ONLY the CTE named '{cte_name}' in your previous solution. Keep other CTEs unchanged.\n"
            "Output a complete <solution> that includes the revised CTE.",
        )
        if feedback:
            revised = _revise_from_feedback(
                feedback=feedback,
                artifact_suffix=cte_name,
                messages=messages,
                model=model,
                engine=engine,
                db_path_or_cred=db_path_or_cred,
                out_dir=out_dir,
            )
            if revised:
                final_sql = revised
                # Refresh CTE bodies from adopted solution
                ctes, remainder_sql = parse_ctes_from_sql(final_sql)

        idx_cte += 1

    # Final SELECT refinement if remainder exists
    final_verdict = None
    if remainder_sql and remainder_sql.strip():
        prev_blocks = []
        for c in ctes:
            cname = c.get('name') or ''
            cbody = c.get('body') or ''
            cgoal = extract_goal_from_cte_body(cbody, cname)
            prev_blocks.append(f"-- CTE: {cname}\n-- Goal: {cgoal}\nWITH {cname} AS (\n{cbody}\n)\n")
        previous_ctes_text = "\n\n".join(prev_blocks).strip()
        complete_query = rebuild_sql_from_ctes(ctes, remainder_sql)
        final_out = out_dir / "refiner_final_select.json"
        final_trace = out_dir / "refiner_final_select_trace.txt"
        final_verdict = refiner_run(
            instance_id=inst.instance_id,
            db_id=inst.db,
            user_query=inst.question,
            cte_text=complete_query,
            cte_goal=_goal_with_knowledge(
                f"Final SELECT using {len(ctes)} CTE(s)",
                retrieve_for('final_select', '_final_select', complete_query),
            ),
            previous_ctes=previous_ctes_text,
            predicted_ctes=predicted_cte_hint or None,
            model=refiner_model,
            max_turns=refiner_turns,
            min_required_sql=refiner_min_probes,
            verbose=verbose,
            trace_output_path=str(final_trace),
            use_all_rules=False,
            tribalknowledge_generic_only=tribalknowledge_generic_only,
            external_knowledge=external_knowledge,
            schema_context=schema_context,
        )
        final_out.write_text(json.dumps(final_verdict, indent=2), encoding='utf-8')

        feedback = _feedback_text(
            final_verdict,
            "the final SELECT",
            "\nInstruction: Revise the final SELECT of your previous solution. Keep the CTEs unchanged.\n"
            "Output a complete <solution>.",
        )
        if feedback:
            revised = _revise_from_feedback(
                feedback=feedback,
                artifact_suffix='final_select',
                messages=messages,
                model=model,
                engine=engine,
                db_path_or_cred=db_path_or_cred,
                out_dir=out_dir,
            )
            if revised:
                final_sql = revised

    if tkstore_path:
        (out_dir / 'retrieved_rules.json').write_text(
            json.dumps({
                'tkstore': _store_provenance(tkstore_path),
                'n_ctes': len(ctes),
                'filter_model': filter_model,
                'use_llm_filtering': bool(use_llm_filtering),
                'rule_scope': rule_scope,
                'retrievals': retrievals,
            }, indent=2),
            encoding='utf-8',
        )

    return final_sql, final_verdict

# ----------------- IO / Orchestration -----------------
def load_instances_from_jsonl(jsonl_path: str) -> List[Instance]:
    # Provider/auth setup (mirrors original script behavior)
    configure_llm_env()

    instances: List[Instance] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            instances.append(Instance(
                instance_id=obj.get("instance_id"),
                db=obj.get("db") or obj.get("db_id") or "unknown_db",
                question=obj.get("question") or obj.get("user_query") or "",
                external_knowledge=obj.get("external_knowledge"),
            ))
    return instances


def run_refinement_on_existing_outputs(args) -> int:
    """Run refinement on existing output directories, loading execution_query.sql instead of regenerating.

    Refines existing outputs and saves to a new directory (specified by --refine-output-dir).

    Returns the number of instances still unrefined, which the caller turns into the exit
    code. `run_steps` stops the pipeline on the first failing step, and that guard is
    inert unless failures show up there: a half-refined arm otherwise flows straight into
    the next one and the comparison comes out looking like a result.
    """
    import shutil
    
    source_dir = Path(args.refine_output)
    
    if not source_dir.exists() or not source_dir.is_dir():
        print(f"❌ Source directory does not exist: {source_dir}")
        sys.exit(1)
    
    # Use --output-dir if specified, otherwise default to source + '_withrefined'
    if args.refine_output_dir:
        dest_dir = Path(args.refine_output_dir)
    else:
        dest_dir = Path(str(source_dir).rstrip('/') + '_withrefined')
    
    # The directory-level marker is a receipt, never a skip decision: it records the
    # instances of one pass, and the source can grow afterwards. Short-circuiting on it
    # made a 3-instance rehearsal silently turn the arm into a no-op when the same
    # prefix was scaled up. The per-instance markers below are the resume state.

    # Resume rather than wipe: an existing directory can hold most of a 14-hour pass.
    # Deleting it is left to the caller (`rm -rf`) so it cannot happen by accident.
    if dest_dir.exists():
        print(f"🔄 Resuming into existing directory: {dest_dir}")

    print(f"📋 Syncing {source_dir} -> {dest_dir}")
    instance_dirs = _sync_instance_dirs(source_dir, dest_dir)
    print(f"✅ Sync complete")

    if not instance_dirs:
        print(f"❌ No instance directories found in {dest_dir}")
        sys.exit(1)
    
    print(f"📂 Found {len(instance_dirs)} instance directories")
    print(f"🔍 Running refinement with max_turns=25")
    
    # Load instances from JSONL to get metadata
    all_instances_list = load_instances_from_jsonl(args.jsonl_path)
    instances_by_id = {inst.instance_id: inst for inst in all_instances_list}
    
    # Load predicted hints
    cte_map = load_predicted_cte_briefs(args.predicted_cte_briefs_csv) if args.predicted_cte_briefs_csv else {}
    schema_map = load_predicted_tables_columns(args.predicted_tables_columns_csv) if args.predicted_tables_columns_csv else {}
    
    processed_count = 0
    failed_count = 0
    skipped_count = 0
    
    for inst_dir in sorted(instance_dirs):
        if _instance_is_refined(inst_dir):
            print(f"⏭️  Already refined, skipping: {inst_dir.name}")
            skipped_count += 1
            continue

        # Extract instance_id from directory name (format: instanceid_timestamp)
        # Handle both sf###_timestamp and sf_bq###_timestamp formats
        dir_name = inst_dir.name
        # Split on underscore and find where timestamp starts (8 digits)
        parts = dir_name.split('_')
        # The timestamp parts are the last 2 elements (date and time)
        # So instance_id is everything except the last 2 parts
        instance_id = '_'.join(parts[:-2]) if len(parts) >= 3 else parts[0]
        
        print(f"\n{'='*80}")
        print(f"🔍 Refining: {instance_id} (from {dir_name})")
        print(f"{'='*80}")
        
        # Check if execution_query.sql exists
        exec_query_path = inst_dir / "execution_query.sql"
        if not exec_query_path.exists():
            print(f"⚠️  No execution_query.sql found in {inst_dir}, skipping...")
            failed_count += 1
            continue
        
        # Load existing SQL
        try:
            existing_sql = exec_query_path.read_text(encoding="utf-8").strip()
            if not existing_sql:
                print(f"⚠️  execution_query.sql is empty in {inst_dir}, skipping...")
                failed_count += 1
                continue
        except Exception as e:
            print(f"❌ Error reading execution_query.sql: {e}")
            failed_count += 1
            continue
        
        # Get instance metadata
        inst = instances_by_id.get(instance_id)
        if not inst:
            print(f"⚠️  Instance {instance_id} not found in JSONL, skipping...")
            failed_count += 1
            continue
        
        print(f"📊 Database: {inst.db}")
        print(f"❓ Question: {inst.question[:200]}..." if len(inst.question) > 200 else f"❓ Question: {inst.question}")
        print(f"📝 Loaded existing SQL ({len(existing_sql)} chars)")
        
        # Copy GT SQL if it exists
        gt_sql_path = Path(f"evaluation/gold/sql/{instance_id}.sql")
        if gt_sql_path.exists() and not (inst_dir / f"{instance_id}.sql").exists():
            shutil.copy(gt_sql_path, inst_dir / f"{instance_id}.sql")
            print(f"📄 Copied GT SQL")
        
        # Get predicted CTE hint
        predicted_cte_hint = cte_map.get(instance_id)
        
        # Infer engine and resolve DB path
        engine = infer_engine(instance_id)
        db_path_or_cred = None
        if engine == "sqlite":
            db_path_or_cred = resolve_sqlite_db_path(instance_id, inst.db)
            if not db_path_or_cred:
                print(f"❌ Could not resolve SQLite DB for {instance_id}")
                failed_count += 1
                continue
        
        # Run refinement
        try:
            # Convert --tribalknowledge-all-scopes flag to tribalknowledge_generic_only parameter
            tribalknowledge_generic_only = not getattr(args, 'tribalknowledge_all_scopes', False)
            # Load external knowledge and schema context for the instance
            is_snowflake = infer_engine(instance_id) == "snowflake"
            schema_context = load_snowflake_schema_context(inst.db) if is_snowflake else None
            external_knowledge = load_external_knowledge(inst.instance_id, inst.external_knowledge)
            
            # Reconstruct minimal message history for cooperative revision
            # The agent needs context to understand what it's revising
            predicted_schema_hint = schema_map.get(instance_id)
            system_msg = get_system_prompt(instance_id, train_context_file=None)  # TEMP EXPERIMENT: no train context in refinement
            user_msg = build_user_message(
                inst, predicted_cte_hint, predicted_schema_hint,
                schema_context=schema_context,
                external_knowledge=external_knowledge,
                expected_output_format=None  # Not needed for revision
            )
            messages = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": f"<solution>\n{existing_sql}\n</solution>"}
            ]
            
            final_sql, refiner_verdict = perform_refinement_and_revision(
                inst=inst,
                final_sql=existing_sql,
                predicted_cte_hint=predicted_cte_hint,
                engine=engine,
                db_path_or_cred=db_path_or_cred,
                messages=messages,  # Now has context for revision
                out_dir=inst_dir,
                model=args.model,
                verbose=bool(args.verbose),
                tribalknowledge_generic_only=tribalknowledge_generic_only,
                external_knowledge=external_knowledge,
                schema_context=schema_context,
                **_knowledge_options(args),
                **_refiner_options(args),
            )
            
            # Don't save _refined files - refiner changes are already in _after_ files
            # Just save the verdict
            print(f"✅ Refinement complete for {instance_id}")
            
            if refiner_verdict is not None:
                (inst_dir / "refiner_verdict.json").write_text(
                    json.dumps(refiner_verdict, indent=2), encoding="utf-8"
                )
            
            # Choose and mark final artifacts based on precedence
            last_cte = None
            try:
                # Try to extract the last CTE name from the final SQL
                from src.utils.agent_utils import parse_ctes_from_sql
                ctes, _ = parse_ctes_from_sql(final_sql)
                if ctes:
                    last_cte = ctes[-1].get('name')
            except Exception:
                pass
            _choose_and_mark_final_artifacts(inst_dir, last_cte_name=last_cte)
            _mark_instance_refined(inst_dir)
            
            processed_count += 1
            
        except Exception as e:
            print(f"❌ Refinement failed for {instance_id}: {e}")
            if args.verbose:
                import traceback
                traceback.print_exc()
            failed_count += 1
    
    # Summary
    print(f"\n{'='*80}")
    print("📊 REFINEMENT SUMMARY")
    print(f"{'='*80}")
    print(f"✅ Successfully refined: {processed_count}/{len(instance_dirs)}")
    print(f"⏭️  Already refined: {skipped_count}/{len(instance_dirs)}")
    print(f"❌ Failed: {failed_count}/{len(instance_dirs)}")
    print(f"📂 Results saved to: {dest_dir}/")
    print()
    
    # A receipt that this pass covered every instance it saw, for reading a finished run
    # after the fact. Resume does not consult it; see the note where dest_dir is resolved.
    if failed_count:
        print(f"⚠️  {failed_count} instance(s) still unrefined; rerun the same command to retry")
        return failed_count
    (dest_dir / REFINEMENT_MARKER).write_text(
        f"Refinement completed at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Refined this pass: {processed_count}/{len(instance_dirs)}\n"
        f"Skipped as already refined: {skipped_count}/{len(instance_dirs)}\n",
        encoding='utf-8')
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SQL Agent Runner")
    p.add_argument("--instance-id", action="append", default=[], help="Instance ID to run; can repeat")
    p.add_argument("--run-all-from-file", action="store_true", help="Run all instances from JSONL path")
    p.add_argument("--rule-scope", choices=RULE_SCOPES, default='all',
                   help="Which rule scopes may reach the refiner. 'db' drops the generic "
                        "rules that made up 95%% of what the reference run injected")
    p.add_argument("--refiner-turns", type=int, default=DEFAULT_REFINER_TURNS,
                   help="Probing turns per fragment for the refiner (upstream tkboost.sql uses 5)")
    p.add_argument("--refiner-min-probes", type=int, default=None,
                   help=f"Probes required before the refiner's verdict is accepted "
                        f"(default: run_refiner's own {DEFAULT_MIN_PROBES}; upstream tkboost.sql passes 3)")
    p.add_argument("--split", type=str, default=None, help="Split file listing the instance IDs to run")
    p.add_argument("--split-offset", type=int, default=0, help="Skip this many IDs of --split, for batching")
    p.add_argument("--split-limit", type=int, default=None, help="Run at most this many IDs of --split, for batching")
    p.add_argument("--jsonl-path", default="data/spider2-lite.jsonl", help="JSONL path with instances")
    # Engine and credential inference from instance_id; no explicit args required
    p.add_argument("--model", default="azure/gpt-4.1", help="LLM model")
    p.add_argument("-c", "--predicted-cte-briefs-csv", default=None, help="CSV path for predicted CTE briefs")
    p.add_argument("-t", "--predicted-tables-columns-csv", default=None, help="CSV path for predicted tables/columns")
    p.add_argument("-v", "--refine-cte", action="store_true", help="Run CTE refiner on final SELECT")
    p.add_argument("--refine-output", type=str, default=None, help="Path to existing output directory to run refinement on (skips initial agent generation)")
    p.add_argument("--refine-output-dir", type=str, default=None, help="Destination directory for refinement results (defaults to source + '_withrefiner')")
    p.add_argument("--tribalknowledge-all-scopes", action="store_true", help="Include both generic and database-specific tribalknowledge rules (default: generic only)")
    p.add_argument("--tkstore", type=str, default=None, help="TK-Store CSV to retrieve rules from; requires --refine-cte or --refine-output")
    p.add_argument("--no-llm-filtering", action="store_true", help="Skip the FilterKnowledge LLM step of retrieval (ablation only)")
    p.add_argument("--filter-model", type=str, default="gpt-4.1", help="Model used for the FilterKnowledge step")
    p.add_argument("--out-base", default="outputs_cleaned", help="Base output directory")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    # TEMP EXPERIMENT: Add train context file
    p.add_argument("--train-context-file", type=str, default=None, help="[TEMP EXPERIMENT] Path to file with train SQL examples to prepend to system prompt")
    return p


REFINEMENT_MARKER = "refinement_complete.marker"


def _instance_is_refined(inst_dir: Path) -> bool:
    return (Path(inst_dir) / REFINEMENT_MARKER).is_file()


def _mark_instance_refined(inst_dir: Path) -> None:
    (Path(inst_dir) / REFINEMENT_MARKER).write_text(
        f"Refinement completed at {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding='utf-8'
    )


def _has_agent_output(inst_dir: Path) -> bool:
    """Whether this instance's agent loop ran to completion.

    `execution_query.sql` is only written once the loop returns, and as `final_sql or ""`
    -- so a directory with the file missing or empty is one the agent never finished.
    """
    sql_file = Path(inst_dir) / "execution_query.sql"
    try:
        return bool(sql_file.read_text(encoding='utf-8').strip())
    except (OSError, UnicodeDecodeError):
        return False


def _instance_dirs_in(base: Path) -> List[Path]:
    base = Path(base)
    if not base.is_dir():
        return []
    return sorted(d for d in base.iterdir() if d.is_dir() and not d.name.startswith('.'))


def _discard_incomplete_instance_dirs(out_base: Path) -> List[str]:
    """Drop leftovers of instances this runner started but never finished.

    An interrupted instance leaves a directory with no `execution_query.sql`. The rerun
    writes a fresh timestamped directory instead of reusing it, so the leftover would
    travel into both arms, where refinement can only count it as a failure -- keeping
    `Failed: n/86` non-zero however often the arm is rerun, and withholding the
    directory receipt with it. Discarding is safe by construction: this is the exact
    complement of the `_has_completed_output` criterion for the agent path.
    """
    discarded = []
    for d in _instance_dirs_in(out_base):
        if not _has_agent_output(d):
            shutil.rmtree(d)
            discarded.append(d.name)
    return discarded


def _sync_instance_dirs(source_dir: Path, dest_dir: Path) -> List[Path]:
    """Mirror the instance directories of `source_dir` into `dest_dir`.

    Copying the tree in one shot is what made a refinement pass all-or-nothing: an
    instance already present in `dest_dir` may hold hours of finished work, so it is
    left untouched and only missing instances are copied.

    Instances the agent never finished are skipped: there is no SQL to refine, and
    carrying them in would only inflate this pass's failure count.

    A marker that comes in with a fresh copy is dropped. It records that some *other*
    pass refined that instance, and trusting it would skip the work of this one --
    yielding an arm that is a plain copy of its input, silently.
    """
    source_dir, dest_dir = Path(source_dir), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    for src in _instance_dirs_in(source_dir):
        dst = dest_dir / src.name
        if dst.exists() or not _has_agent_output(src):
            continue
        shutil.copytree(src, dst)
        inherited = dst / REFINEMENT_MARKER
        if inherited.exists():
            inherited.unlink()

    return _instance_dirs_in(dest_dir)


def _has_completed_output(instance_id: str, out_base: Path, require_refinement: bool = False) -> bool:
    """Whether `instance_id` can be skipped on a rerun.

    `execution_query.sql` is written before refinement starts, so when refinement is
    part of the run it cannot stand in for "finished": an instance interrupted during
    refinement would be skipped forever and quietly ship unrefined.
    """
    for dir_path in _instance_dirs_in(out_base):
        if not dir_path.name.startswith(f"{instance_id}_"):
            continue
        if not _has_agent_output(dir_path):
            continue
        if require_refinement and not _instance_is_refined(dir_path):
            continue
        return True
    return False


def _store_provenance(tkstore_path: str) -> Dict[str, object]:
    """Identify the store a run actually read, for `retrieved_rules.json`.

    The arms of an experiment can differ only by which store was passed, and two
    arms may name the store identically, so the path alone is not enough. A file
    that cannot be read yields null fields rather than losing the whole report;
    `_knowledge_options` already rejects a missing path at the CLI boundary.
    """
    provenance: Dict[str, object] = {'path': str(tkstore_path), 'sha1': None, 'n_rules': None}
    try:
        raw = Path(tkstore_path).read_bytes()
    except OSError:
        return provenance

    provenance['sha1'] = hashlib.sha1(raw).hexdigest()
    text = raw.decode('utf-8', errors='replace')
    provenance['n_rules'] = max(len(list(csv.DictReader(io.StringIO(text)))), 0)
    return provenance


def _requested_instance_ids(args) -> Optional[List[str]]:
    """Instance ids to run, in file order, or None for every instance in the JSONL.

    Raises so `main` can report a parser error; picking one of several conflicting
    selectors would quietly run the wrong instance set.
    """
    used = [name for name, given in (
        ("--instance-id", bool(args.instance_id)),
        ("--run-all-from-file", bool(args.run_all_from_file)),
        ("--split", bool(args.split)),
    ) if given]
    if len(used) > 1:
        raise ValueError(f"{', '.join(used)} are mutually exclusive; pass exactly one")
    if not used:
        raise ValueError("provide one of --instance-id, --split, or --run-all-from-file")

    batching = args.split_offset or args.split_limit is not None
    if batching and not args.split:
        raise ValueError("--split-offset and --split-limit only apply to --split")

    if args.run_all_from_file:
        return None
    if not args.split:
        return list(args.instance_id)

    ids = load_split(args.split)
    if args.split_offset < 0:
        raise ValueError(f"--split-offset cannot be negative: {args.split_offset}")
    if args.split_limit is not None and args.split_limit < 1:
        raise ValueError(f"--split-limit must be positive: {args.split_limit}")
    if args.split_offset >= len(ids):
        raise ValueError(
            f"--split-offset {args.split_offset} is beyond the {len(ids)} ids in {args.split}"
        )
    end = None if args.split_limit is None else args.split_offset + args.split_limit
    return ids[args.split_offset:end]


def _refiner_options(args) -> Dict[str, object]:
    """Refiner budget kwargs for `perform_refinement_and_revision`.

    The two settings are coupled: `run_refiner` withholds its verdict until
    `min_required_sql` probes have run, one per turn, so a minimum the turn budget
    cannot reach makes a verdict unreachable. It would then fall through to the
    `no_verdict` fallback, whose status is `issues` and therefore triggers a rewrite of
    a fragment nobody diagnosed -- a silent, systematic corruption. Rejecting the
    combination up front turns that into a CLI error.
    """
    if args.refiner_turns < 1:
        raise ValueError(f"--refiner-turns must be positive: {args.refiner_turns}")

    effective_min = DEFAULT_MIN_PROBES if args.refiner_min_probes is None else args.refiner_min_probes
    if effective_min < 0:
        raise ValueError(f"--refiner-min-probes cannot be negative: {args.refiner_min_probes}")
    if effective_min >= args.refiner_turns:
        raise ValueError(
            f"--refiner-min-probes {effective_min} is unreachable within --refiner-turns "
            f"{args.refiner_turns}: probes run one per turn and at least one turn is needed "
            f"for the verdict itself. Lower --refiner-min-probes "
            f"(upstream tkboost.sql pairs 5 turns with 3 probes)."
        )
    return {
        'refiner_turns': args.refiner_turns,
        'refiner_min_probes': args.refiner_min_probes,
    }


def _knowledge_options(args) -> Dict[str, object]:
    """Knowledge kwargs for `perform_refinement_and_revision`, or {} when disabled."""
    if not args.tkstore:
        if args.rule_scope != 'all':
            raise ValueError(
                f"--rule-scope {args.rule_scope} has no effect without --tkstore; "
                "the run would look like a completed ablation having retrieved nothing"
            )
        return {}
    if not (args.refine_cte or args.refine_output):
        raise ValueError(
            "--tkstore has no effect without --refine-cte or --refine-output, "
            "because retrieval only runs during refinement"
        )
    if not Path(args.tkstore).exists():
        raise ValueError(f"--tkstore path does not exist: {args.tkstore}")
    return {
        'tkstore_path': args.tkstore,
        'use_llm_filtering': not args.no_llm_filtering,
        'filter_model': args.filter_model,
        'rule_scope': args.rule_scope,
    }


def main():
    p = _build_parser()
    args = p.parse_args()
    try:
        _knowledge_options(args)
        _refiner_options(args)
    except ValueError as e:
        p.error(str(e))

    # Refinement-only mode: load existing outputs and run refiner
    if args.refine_output:
        if run_refinement_on_existing_outputs(args):
            sys.exit(1)
        return

    try:
        requested_ids = _requested_instance_ids(args)
    except (ValueError, FileNotFoundError) as e:
        p.error(str(e))

    all_instances = load_instances_from_jsonl(args.jsonl_path)
    instances: List[Instance] = []
    if requested_ids is None:
        instances = all_instances
    else:
        by_id = {inst.instance_id: inst for inst in all_instances}
        instances = [by_id[i] for i in requested_ids if i in by_id]
        missing = [i for i in requested_ids if i not in by_id]
        if missing:
            print(f"⚠️  Missing instances in JSONL: {missing}")

    # Load predicted hints
    cte_map = load_predicted_cte_briefs(args.predicted_cte_briefs_csv) if args.predicted_cte_briefs_csv else {}
    schema_map = load_predicted_tables_columns(args.predicted_tables_columns_csv) if args.predicted_tables_columns_csv else {}

    # Ensure output base
    out_base = Path(args.out_base)
    ensure_dir(out_base)

    abandoned = _discard_incomplete_instance_dirs(out_base)
    if abandoned:
        print(f"\n🧹 Discarded {len(abandoned)} unfinished directory(ies) from an earlier run: {abandoned}")

    # Filter instances: skip those with existing outputs
    completed = []
    to_run = []
    for inst in instances:
        if _has_completed_output(inst.instance_id, out_base, require_refinement=bool(args.refine_cte)):
            completed.append(inst.instance_id)
        else:
            to_run.append(inst)
    
    if completed:
        print(f"\n⏭️  Skipping {len(completed)} already completed instance(s): {completed}")
    if to_run:
        print(f"\n▶️  Running {len(to_run)} instance(s): {[inst.instance_id for inst in to_run]}")
    if not to_run:
        print("\n✅ All instances already completed. Nothing to run.")
        return

    for inst in to_run:
        print(f"\n{'='*80}")
        print(f"🚀 Running SQL Agent for Instance: {inst.instance_id}")
        print(f"{'='*80}")
        print(f"📊 Database: {inst.db}")
        print(f"❓ Question: {inst.question[:200]}..." if len(inst.question) > 200 else f"❓ Question: {inst.question}")
        
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = out_base / f"{inst.instance_id}_{ts}"
        ensure_dir(out_dir)

        # Determine if this is a Snowflake instance and load schema context
        is_snowflake = inst.instance_id.lower().startswith('sf')
        predicted_cte_hint = cte_map.get(inst.instance_id)
        predicted_schema_hint = schema_map.get(inst.instance_id)
        schema_context = load_snowflake_schema_context(inst.db) if is_snowflake else None
        external_knowledge = load_external_knowledge(inst.instance_id, inst.external_knowledge)
        
        if is_snowflake:
            print(f"❄️  Snowflake instance detected - using Snowflake prompt")
            if schema_context:
                print(f"📋 Schema context loaded for {inst.db}")
        if external_knowledge:
            print(f"📄 External knowledge loaded from {inst.external_knowledge}")

        # Infer engine and resolve DB path for SQLite
        engine = infer_engine(inst.instance_id)
        db_path_or_cred = None
        if engine == "sqlite":
            db_path_or_cred = resolve_sqlite_db_path(inst.instance_id, inst.db)
            if not db_path_or_cred:
                print(f"❌ Could not resolve SQLite DB for {inst.instance_id}")
                continue

        # Load ground truth
        gt_query, gt_result, all_col_names = load_ground_truth(inst.instance_id)
        
        # Derive expected output format from GT CSV column names
        # If multiple variants exist (_a, _b, etc.), provide all as options
        expected_output_format = None
        if all_col_names:
            if len(all_col_names) == 1:
                expected_output_format = f"Expected Output Format: columns={all_col_names[0]} (use this exact order)."
            else:
                # Multiple valid output formats
                variants_str = "\n".join([f"  Option {i+1}: {cols}" for i, cols in enumerate(all_col_names)])
                expected_output_format = f"Expected Output Format (multiple valid options):\n{variants_str}\n(Choose one option and use that exact column order)."
            if args.verbose:
                print(f"\n🧾 {expected_output_format}")
        
        # Run agent
        final_sql, headers, rows, messages, executor = run_agent(
            inst=inst,
            engine=engine,
            db_path_or_cred=db_path_or_cred,
            model=args.model,
            predicted_cte_hint=predicted_cte_hint,
            predicted_schema_hint=predicted_schema_hint,
            schema_context=schema_context,
            external_knowledge=external_knowledge,
            expected_output_format=expected_output_format,
            max_turns=25,
            train_context_file=args.train_context_file,  # TEMP EXPERIMENT
            verbose=bool(args.verbose),
        )
        
        # Save original agent outputs immediately
        (out_dir / "execution_query.sql").write_text(final_sql or "", encoding="utf-8")
        write_csv(headers, rows, out_dir / "execution_result.csv")
        (out_dir / "messages.json").write_text(json.dumps(messages, indent=2), encoding="utf-8")
        
        # Generate and save processed trace
        processed_trace = generate_processed_trace(messages)
        (out_dir / "processed_trace.txt").write_text(processed_trace, encoding="utf-8")
        
        # Save ground truth query
        if gt_query:
            (out_dir / "gt_query.sql").write_text(gt_query, encoding="utf-8")
        
        # Also copy GT SQL from evaluation/gold/sql if it exists
        gt_sql_path = Path(f"evaluation/gold/sql/{inst.instance_id}.sql")
        if gt_sql_path.exists():
            shutil.copy(gt_sql_path, out_dir / f"{inst.instance_id}.sql")
        
        # Save ground truth result
        if gt_result and all_col_names:
            # Save as JSON
            (out_dir / "gt_result.json").write_text(
                json.dumps({"instance_id": inst.instance_id, "gt_result": gt_result}, indent=2),
                encoding="utf-8"
            )
            # Save as CSV with proper column names
            try:
                df_gt = pd.DataFrame(gt_result, columns=all_col_names[0] if all_col_names else None)
                df_gt.to_csv(out_dir / "gt_result.csv", index=False)
            except Exception as e:
                print(f"⚠️  Warning: Could not save GT result as CSV: {e}")
        
        # Save execution result as JSON
        if headers and rows:
            result_json = {
                "instance_id": inst.instance_id,
                "headers": headers,
                "rows": [[make_json_serializable(val) for val in row] for row in rows],
            }
            (out_dir / "execution_result.json").write_text(json.dumps(result_json, indent=2), encoding="utf-8")
        
        # Close connection after each instance (important for Snowflake persistent connections)
        if hasattr(executor, 'close'):
            executor.close()
        
        print(f"\n✅ Done {inst.instance_id} → {out_dir}")

        # Refiner step (optional) — per-CTE refinement with cooperative revision, then final SELECT refinement
        refiner_verdict = None
        if args.refine_cte and final_sql:
            try:
                final_sql, refiner_verdict = perform_refinement_and_revision(
                    inst=inst,
                    final_sql=final_sql,
                    predicted_cte_hint=predicted_cte_hint,
                    engine=engine,
                    db_path_or_cred=db_path_or_cred,
                    messages=messages,
                    out_dir=out_dir,
                    model=args.model,
                    verbose=bool(args.verbose),
                    external_knowledge=external_knowledge,
                    schema_context=schema_context,
                    **_knowledge_options(args),
                    **_refiner_options(args),
                )
            except Exception as e:
                if args.verbose:
                    print(f"⚠️  Refiner step failed: {e}")
            # Don't save _refined files - refiner changes are already saved as _after_ files
            if refiner_verdict is not None:
                (out_dir / "refiner_verdict.json").write_text(json.dumps(refiner_verdict, indent=2), encoding="utf-8")
            
            # Choose and mark final artifacts based on precedence ONLY if refinement was performed
            last_cte = None
            try:
                # Try to extract the last CTE name from the final SQL
                from src.utils.agent_utils import parse_ctes_from_sql
                ctes, _ = parse_ctes_from_sql(final_sql)
                if ctes:
                    last_cte = ctes[-1].get('name')
            except Exception:
                pass
            _choose_and_mark_final_artifacts(out_dir, last_cte_name=last_cte)
            _mark_instance_refined(out_dir)

        if args.verbose:
            print(f"✅ Done {inst.instance_id} → {out_dir}")

    # An instance without agent output is dropped from both arms by `_sync_instance_dirs`,
    # so staying quiet here would let the pipeline compare a smaller sample and present
    # it as a result. `run_steps` only stops on a non-zero exit.
    unfinished = [inst.instance_id for inst in to_run
                  if not _has_completed_output(inst.instance_id, out_base)]
    if unfinished:
        print(f"\n❌ {len(unfinished)} instance(s) produced no SQL: {unfinished}")
        print("   Rerun the same command to retry them; finished instances are skipped.")
        sys.exit(1)


if __name__ == "__main__":
    main()


