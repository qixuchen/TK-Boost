"""Exporting accepted divergences from reflector records and loading them back."""

import json

from tkstore.dataclaw.divergence_store import export_accepted, load_divergences, write_divergences
from tkstore.dataclaw.reflector_io import Divergence, Evidence, Reproduction
from tkstore.dataclaw.scope import ColumnRef

OPS = "enterprise/company_operation_status.csv"


def _divergence(index, fact):
    return {
        "index": index,
        "divergence": "d",
        "needed": "n",
        "scope": "multi_column",
        "tables": [],
        "columns": [[OPS, "targetUnit"], [OPS, "value"]],
        "fact": fact,
        "category": "6. Units that vary per row",
        "evidence": [{"probe": 2, "start": 4, "end": 5, "excerpt": "千万元 -> 0.1"}],
        "reproduced": [
            {"key": "k", "value": 63.7, "probe": 3, "start": 7, "end": 7, "semantic_match": None, "excerpt": "63.7"}
        ],
        "kind": "data",
        "errors": [],
    }


def _record(path, task_id, run_dir, accepted, logged=()):
    record = {
        "task_id": task_id,
        "run_dir": run_dir,
        "model": "openai/glm-5.2",
        "accepted": [{"divergence": d, "status": "accepted", "reasons": []} for d in accepted],
        "logged": [{"divergence": d, "status": "logged", "reasons": []} for d in logged],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return path


def test_exports_only_accepted_in_a_stable_order(tmp_path):
    b = _record(tmp_path / "b/task_185.json", "task_185_x", "glm-5.2_20260710_1634_850743", [_divergence(1, "B")])
    a = _record(
        tmp_path / "a/task_011.json", "task_011_x", "glm-5.2_20260811_0937_b794e1",
        [_divergence(1, "A1"), _divergence(2, "A2")], logged=[_divergence(3, "L")],
    )
    records = export_accepted([b, a], root=tmp_path)
    assert [r["divergence"]["fact"] for r in records] == ["A1", "A2", "B"]
    assert [r["divergence_id"] for r in records] == [
        "task_011__b794e1__1", "task_011__b794e1__2", "task_185__850743__1",
    ]
    assert records[0]["source_record"] == "a/task_011.json"
    assert records[0]["task_id"] == "task_011_x" and records[0]["model"] == "openai/glm-5.2"


def test_same_run_accepted_twice_gets_distinct_ids(tmp_path):
    first = _record(tmp_path / "a/r.json", "task_185_x", "glm-5.2_1_850743", [_divergence(1, "first")])
    second = _record(tmp_path / "b/r.json", "task_185_x", "glm-5.2_1_850743", [_divergence(1, "second")])
    ids = [r["divergence_id"] for r in export_accepted([second, first], root=tmp_path)]
    assert ids == ["task_185__850743__1", "task_185__850743__1~2"]


def test_round_trip_restores_divergence_objects(tmp_path):
    src = _record(tmp_path / "a/r.json", "task_011_x", "glm-5.2_1_b794e1", [_divergence(1, "A1")])
    out = tmp_path / "divergences.jsonl"
    write_divergences(export_accepted([src], root=tmp_path), out)
    (item,) = load_divergences(out)
    assert item.divergence_id == "task_011__b794e1__1"
    d = item.divergence
    assert isinstance(d, Divergence)
    assert d.columns == [ColumnRef(OPS, "targetUnit"), ColumnRef(OPS, "value")]
    assert d.evidence == [Evidence(2, 4, 5, "千万元 -> 0.1")]
    assert d.reproduced == [Reproduction("k", 63.7, 3, 7, 7, None, "63.7")]
    assert "千万元" in out.read_text(encoding="utf-8")
