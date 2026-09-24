"""`populate_from_output_dir`, the Algorithm 2 entry point.

Every LLM stage is replaced by a stub so the tests describe how the experience
tuple is assembled and persisted, not what a model happens to say.
"""

import json

import pandas as pd
import pytest

from tkboost import TKStore
from tkstore.populate import populate_from_output_dir


GOLD_ROWS = {"name": ["a", "b"], "n": [1, 2]}


@pytest.fixture
def gold_dir(tmp_path):
    root = tmp_path / "gold"
    (root / "exec_result").mkdir(parents=True)
    (root / "sql").mkdir()
    pd.DataFrame(GOLD_ROWS).to_csv(root / "exec_result" / "local900.csv", index=False)
    (root / "sql" / "local900.sql").write_text("SELECT name, n FROM t;", encoding="utf-8")
    (root / "spider2lite_eval.jsonl").write_text(
        json.dumps({"instance_id": "local900", "condition_cols": [], "ignore_order": False}) + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def jsonl_path(tmp_path):
    path = tmp_path / "spider2-lite.jsonl"
    path.write_text(
        json.dumps(
            {
                "instance_id": "local900",
                "db": "TestDB",
                "question": "How many of each name?",
                "external_knowledge": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def make_output_dir(
    tmp_path,
    agent_rows,
    *,
    trace="TRACE TEXT",
    messages=None,
    agent_sql="SELECT name, n FROM t;",
):
    """An agent output directory shaped like the runner's."""
    out = tmp_path / "outputs" / "local900_20260911_120000"
    out.mkdir(parents=True)
    (out / "execution_query.sql").write_text(agent_sql, encoding="utf-8")
    if agent_rows is None:
        (out / "execution_result.csv").write_text("", encoding="utf-8")
    else:
        pd.DataFrame(agent_rows).to_csv(out / "execution_result.csv", index=False)
    pd.DataFrame(GOLD_ROWS).to_csv(out / "gt_result.csv", index=False)
    (out / "processed_trace.txt").write_text(trace, encoding="utf-8")
    (out / "messages.json").write_text(json.dumps(messages or []), encoding="utf-8")
    return out


@pytest.fixture
def llm_calls(monkeypatch):
    """Record every LLM stage and return canned output."""
    calls = {"diff": [], "rules": [], "tagger": []}

    def fake_diff(**kwargs):
        calls["diff"].append(kwargs)
        return "DIFF TEXT\nCLEAN_SUMMARY: the agent dropped a filter"

    def fake_rules(*args, **kwargs):
        calls["rules"].append({"args": args, "kwargs": kwargs})
        return "DATABASE_MEMORIES:\n- check the filter\nGENERIC_MEMORIES:\n- check filters"

    def fake_tagger(**kwargs):
        calls["tagger"].append(kwargs)
        return {
            "index_rows": [
                {
                    "scope": "db",
                    "sql_operations": ["select", "where"],
                    "table": ["t"],
                    "column": ["n"],
                    "data_type": ["int"],
                    "nulls": "No",
                    "rule": "list-valued tags",
                },
                {
                    "scope": "generic",
                    "sql_operations": "where",
                    "table": "all",
                    "column": "all",
                    "data_type": "all",
                    "nulls": "all",
                    "rule": "check filters",
                },
                # Untyped so the retrieval test isolates the db dimension: the
                # data_type and nulls filters are separate concerns.
                {
                    "scope": "db",
                    "sql_operations": "select",
                    "table": "all",
                    "column": "all",
                    "data_type": "all",
                    "nulls": "all",
                    "rule": "untyped db rule",
                },
            ]
        }

    monkeypatch.setattr("tkstore.populate.generate_memory_diff_first_turn", fake_diff)
    monkeypatch.setattr("tkstore.populate.generate_rules_from_diff", fake_rules)
    monkeypatch.setattr("tkstore.populate.generate_tagged_memories_json", fake_tagger)
    return calls


def run(out_dir, gold_dir, jsonl_path, store, **kwargs):
    return populate_from_output_dir(
        output_dir=str(out_dir),
        jsonl_path=str(jsonl_path),
        gold_dir=str(gold_dir),
        gold_sql_dir=str(gold_dir / "sql"),
        store=str(store),
        db_path_or_cred="/tmp/unused.sqlite",
        verbose=False,
        **kwargs,
    )


def test_correct_instance_is_skipped_without_any_llm_call(tmp_path, gold_dir, jsonl_path, llm_calls):
    """Algorithm 3 line 1 takes the incorrect SQL, so a correct agent run has
    nothing to learn from and must not cost a model call."""
    out = make_output_dir(tmp_path, GOLD_ROWS)
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    result = run(out, gold_dir, jsonl_path, store)

    assert result["skipped"] == "correct"
    assert result["rule_count"] == 0
    assert llm_calls == {"diff": [], "rules": [], "tagger": []}
    assert TKStore(str(store)).rows() == []


def test_incorrect_instance_is_populated(tmp_path, gold_dir, jsonl_path, llm_calls):
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    result = run(out, gold_dir, jsonl_path, store)

    assert result["skipped"] is None
    assert result["rule_count"] == 3
    assert len(llm_calls["diff"]) == 1


def test_instance_id_and_engine_come_from_the_directory_name(tmp_path, gold_dir, jsonl_path, llm_calls):
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    result = run(out, gold_dir, jsonl_path, store)

    assert result["instance_id"] == "local900"
    assert result["engine"] == "sqlite"
    assert result["db"] == "TestDB"
    assert result["store"] == str(store)


def test_db_scoped_rows_carry_the_instance_db_not_all(tmp_path, gold_dir, jsonl_path, llm_calls):
    """Deviation A7: leaving `db` at the default turns database-specific rules
    into global ones and makes the phase-3 check pass for the wrong reason."""
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    run(out, gold_dir, jsonl_path, store)

    rows = TKStore(str(store)).rows()
    db_rows = [r for r in rows if r["scope"] == "db"]
    assert db_rows, "expected at least one database-scoped rule"
    assert {r["db"] for r in db_rows} == {"TestDB"}


def test_store_keeps_the_ten_column_shape_and_no_question_rows(tmp_path, gold_dir, jsonl_path, llm_calls):
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    run(out, gold_dir, jsonl_path, store)

    rows = TKStore(str(store)).rows()
    assert all(set(r) == set(TKStore.HEADER) for r in rows)
    assert {r["scope"] for r in rows} == {"db", "generic"}
    assert [r for r in rows if r["scope"] == "question"] == []


def test_list_valued_tags_are_joined_not_stringified(tmp_path, gold_dir, jsonl_path, llm_calls):
    """Deviation C15: the tagger emits JSON arrays for table/column/data_type,
    and a bare str() would store the Python repr `['t']`."""
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    run(out, gold_dir, jsonl_path, store)

    db_row = next(r for r in TKStore(str(store)).rows() if r["rule"] == "list-valued tags")
    assert db_row["sql_operations"] == "select;where"
    assert db_row["table"] == "t"
    assert db_row["column"] == "n"
    assert db_row["data_type"] == "int"


def test_retrieval_honours_the_db_filter(tmp_path, gold_dir, jsonl_path, llm_calls):
    """The other half of A7: a db-scoped rule must not surface for a different
    database, which is exactly what `db='all'` would silently do."""
    from tkstore.tagger_index import MemoryRetriever

    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"
    run(out, gold_dir, jsonl_path, store)

    retriever = MemoryRetriever(str(store))
    sql = "SELECT n FROM t WHERE n > 0"

    matched = retriever.retrieve(sql, generic_only=False, db="TestDB")
    assert any(r["scope"] == "db" for r in matched)

    other = retriever.retrieve(sql, generic_only=False, db="SomeOtherDB")
    assert [r for r in other if r["scope"] == "db"] == []


def test_execution_trace_reaches_the_diff(tmp_path, gold_dir, jsonl_path, llm_calls):
    """Deviation A4: the other populate entry point hardcodes the trace away,
    dropping the tau of the paper's experience tuple e = (q, tau, s*)."""
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]}, trace="TURN 1 ... TURN 9")
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    run(out, gold_dir, jsonl_path, store)

    assert llm_calls["diff"][0]["processed_trace_text"] == "TURN 1 ... TURN 9"


def test_gold_sql_comes_from_the_gold_dir_not_the_output_dir(tmp_path, gold_dir, jsonl_path, llm_calls):
    """Deviation C8: the runner does not reliably write gold SQL next to its
    outputs, so `evaluation/gold/sql` is the authority."""
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    (out / "gt_query.sql").write_text("SELECT 'stale copy';", encoding="utf-8")
    (out / "local900.sql").write_text("SELECT 'stale copy';", encoding="utf-8")
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    run(out, gold_dir, jsonl_path, store)

    assert llm_calls["diff"][0]["gold_sql_text"] == "SELECT name, n FROM t;"


def test_result_csvs_are_handed_over_as_markdown_tables(tmp_path, gold_dir, jsonl_path, llm_calls):
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    run(out, gold_dir, jsonl_path, store)

    diff_kwargs = llm_calls["diff"][0]
    assert diff_kwargs["agent_result_csv_text"].startswith("| name | n")
    assert diff_kwargs["gold_result_csv_text"].startswith("| name | n")


def test_external_knowledge_is_forwarded_as_evidence(tmp_path, gold_dir, jsonl_path, llm_calls, monkeypatch):
    monkeypatch.setattr(
        "tkstore.populate.load_external_knowledge",
        lambda instance_id, filename: "RFM buckets are defined as ...",
    )
    jsonl_path.write_text(
        json.dumps(
            {
                "instance_id": "local900",
                "db": "TestDB",
                "question": "How many of each name?",
                "external_knowledge": "RFM.md",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = make_output_dir(tmp_path, {"name": ["a", "b"], "n": [1, 99]})
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    run(out, gold_dir, jsonl_path, store)

    assert llm_calls["diff"][0]["external_knowledge"] == "RFM buckets are defined as ..."
    assert llm_calls["tagger"][0]["evidence"] == "RFM buckets are defined as ..."


def test_failed_agent_sql_reports_the_real_database_error(tmp_path, gold_dir, jsonl_path, llm_calls):
    """A zero-byte result CSV is the strongest correction signal, but the
    runner only prints the final execution error, so populate recovers it by
    re-running the SQL. Handing the model an empty string instead would leave
    it unable to tell a syntax error from an empty result set."""
    import sqlite3

    db_path = tmp_path / "test.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (name TEXT, n INTEGER)")

    out = make_output_dir(tmp_path, None, agent_sql="SELECT name, missing_col FROM t;")
    store = tmp_path / "artifacts" / "tkstore_sqlite.csv"

    populate_from_output_dir(
        output_dir=str(out),
        jsonl_path=str(jsonl_path),
        gold_dir=str(gold_dir),
        gold_sql_dir=str(gold_dir / "sql"),
        store=str(store),
        db_path_or_cred=str(db_path),
        verbose=False,
    )

    agent_result = llm_calls["diff"][0]["agent_result_csv_text"]
    assert agent_result.startswith("SQL_ERROR:")
    assert "missing_col" in agent_result


def test_omitted_db_path_is_resolved_with_the_record_db(tmp_path, llm_calls, monkeypatch):
    """BIRD minidev paths need db_id; populate_split used to call resolve with id only."""
    iid = "minidev0000"
    gold = tmp_path / "gold"
    (gold / "exec_result").mkdir(parents=True)
    (gold / "sql").mkdir()
    pd.DataFrame(GOLD_ROWS).to_csv(gold / "exec_result" / f"{iid}.csv", index=False)
    (gold / "sql" / f"{iid}.sql").write_text("SELECT 1;", encoding="utf-8")
    (gold / "spider2lite_eval.jsonl").write_text(
        json.dumps({"instance_id": iid, "condition_cols": [], "ignore_order": True})
        + "\n",
        encoding="utf-8",
    )
    jsonl = tmp_path / "bird_minidev.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "instance_id": iid,
                "db": "financial",
                "question": "how many?",
                "external_knowledge": "Count rows.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "outputs" / f"{iid}_20260924_103622"
    out.mkdir(parents=True)
    (out / "execution_query.sql").write_text("SELECT 2;", encoding="utf-8")
    pd.DataFrame({"name": ["a", "b"], "n": [1, 99]}).to_csv(
        out / "execution_result.csv", index=False
    )
    pd.DataFrame(GOLD_ROWS).to_csv(out / "gt_result.csv", index=False)
    (out / "processed_trace.txt").write_text("TRACE", encoding="utf-8")
    (out / "messages.json").write_text("[]", encoding="utf-8")

    seen = []

    def fake_resolve(instance_id, db_id=None):
        seen.append((instance_id, db_id))
        return "/tmp/financial.sqlite"

    monkeypatch.setattr("tkstore.populate.resolve_sqlite_db_path", fake_resolve)

    result = populate_from_output_dir(
        output_dir=str(out),
        jsonl_path=str(jsonl),
        gold_dir=str(gold),
        gold_sql_dir=str(gold / "sql"),
        store=str(tmp_path / "artifacts" / "tkstore_bird.csv"),
        verbose=False,
    )

    assert seen == [(iid, "financial")]
    assert result["db"] == "financial"
    assert result["skipped"] is None
