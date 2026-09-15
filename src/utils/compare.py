"""Building the stage 4 comparison table from the three arms.

`delta_knowledge` (arm B - arm A) is the number attributable to tribal knowledge:
both arms carry the refiner and differ only by the store. `delta_paper`
(arm B - bare agent) matches the paper's Fig. 6 but also carries the refiner's own
probing gain, so it is for alignment rather than attribution.

The bare-agent score is read from the arms rather than from the shared agent
directory, because `evaluate.py` deletes duplicate-id directories and the shared
output is the input to both arms. Reading it from both arms also cross-checks that
they really did start from the same query.
"""

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional

FINAL_SELECT = "final_select"


@dataclass(frozen=True)
class Row:
    """One instance across the three arms.

    `paired` is False when the two arms disagree on the bare score, which means they
    did not refine the same starting query and this row's `delta_knowledge` measures
    something other than knowledge.
    """

    instance_id: str
    db: str
    score_bare: int
    score_arm_a: int
    score_arm_b: int
    delta_knowledge: int
    delta_paper: int
    rules_used: List[str]
    n_db_rules: int
    paired: bool = True


@dataclass
class Comparison:
    rows: List[Row] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class GroupSummary:
    """Counts for one reporting group. `improved` / `regressed` are kept apart
    because a net delta of zero can hide one fix and one break."""

    label: str
    n: int
    bare: int
    arm_a: int
    arm_b: int
    improved: int
    regressed: int
    unpaired: int


def _as_int(value: Optional[str]) -> int:
    """`evaluate.py` leaves the cell empty when there is no prediction CSV."""
    text = (value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def load_scores(evals_csv: Path) -> Dict[str, Dict[str, int]]:
    """`instance_id -> {score, score_final}` from an `evaluate.py` output."""
    evals_csv = Path(evals_csv)
    if not evals_csv.is_file():
        raise FileNotFoundError(f"evals.csv not found: {evals_csv}")

    with evals_csv.open("r", encoding="utf-8", newline="") as f:
        return {
            (row.get("instance_id") or "").strip(): {
                "score": _as_int(row.get("score")),
                "score_final": _as_int(row.get("score_final")),
            }
            for row in csv.DictReader(f)
            if (row.get("instance_id") or "").strip()
        }


def _mem_id_key(mem_id: str):
    try:
        return (0, int(mem_id))
    except ValueError:
        return (1, mem_id)


def rules_used(instance_dir: Path) -> List[str]:
    """Rules that could have changed this instance's SQL -- an upper bound.

    A rule counts when the refiner reported `issues` for the fragment it was
    retrieved for *and* the agent's rewrite of that fragment was adopted. There is no
    ground truth available: the verdict never cites rule ids, so a rule the refiner
    read and silently absorbed under an `ok` verdict is indistinguishable from one it
    ignored. Both are excluded.

    Arm A writes no `retrieved_rules.json` at all, which yields an empty list rather
    than an error.
    """
    instance_dir = Path(instance_dir)
    report = instance_dir / "retrieved_rules.json"
    if not report.is_file():
        return []

    retrievals = json.loads(report.read_text(encoding="utf-8")).get("retrievals", [])
    used: List[str] = []
    for retrieval in retrievals:
        # The final-select retrieval is named `_final_select`, but its artifacts are not.
        base = FINAL_SELECT if retrieval.get("stage") == FINAL_SELECT else retrieval.get("name")
        verdict = instance_dir / f"refiner_{base}.json"
        if not verdict.is_file():
            continue
        if json.loads(verdict.read_text(encoding="utf-8")).get("status") != "issues":
            continue
        if not (instance_dir / f"execution_query_after_{base}.sql").is_file():
            continue
        used.extend(retrieval.get("selected", []))

    return sorted(dict.fromkeys(used), key=_mem_id_key)


def db_rule_counts(store_csv: Path) -> Dict[str, int]:
    """Lowercased database name -> number of db-scoped rules it carries.

    Lowercased because retrieval compares `row_db == db.lower()`, and the upstream
    store spells one database both `AdventureWorks` and `adventureworks`.
    """
    store_csv = Path(store_csv)
    if not store_csv.is_file():
        raise FileNotFoundError(f"TK-Store CSV not found: {store_csv}")

    counts: Dict[str, int] = {}
    with store_csv.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("scope") or "").strip().lower() == "generic":
                continue
            db = (row.get("db") or "").strip().lower()
            if db:
                counts[db] = counts.get(db, 0) + 1
    return counts


def instance_dirs_by_id(base: Path) -> Dict[str, Path]:
    """`instance_id -> directory`, taking the latest timestamp when there are several."""
    base = Path(base)
    found: Dict[str, Path] = {}
    for d in sorted(x for x in base.iterdir() if x.is_dir() and not x.name.startswith('.')):
        parts = d.name.split('_')
        instance_id = '_'.join(parts[:-2]) if len(parts) >= 3 else parts[0]
        found[instance_id] = d
    return found


def compare_arms(
    arm_a_dir: Path,
    arm_b_dir: Path,
    store_csv: Path,
    db_of: Mapping[str, str],
) -> Comparison:
    """One row per instance the two arms agree on, plus warnings for what they do not."""
    arm_a_dir, arm_b_dir = Path(arm_a_dir), Path(arm_b_dir)
    scores_a = load_scores(arm_a_dir / "evals.csv")
    scores_b = load_scores(arm_b_dir / "evals.csv")
    counts = db_rule_counts(store_csv)
    dirs_b = instance_dirs_by_id(arm_b_dir)

    result = Comparison()
    for instance_id in sorted(set(scores_a) - set(scores_b)):
        result.warnings.append(f"{instance_id} is scored in {arm_a_dir.name} but not {arm_b_dir.name}")
    for instance_id in sorted(set(scores_b) - set(scores_a)):
        result.warnings.append(f"{instance_id} is scored in {arm_b_dir.name} but not {arm_a_dir.name}")

    for instance_id in sorted(set(scores_a) & set(scores_b)):
        a, b = scores_a[instance_id], scores_b[instance_id]
        if a["score"] != b["score"]:
            result.warnings.append(
                f"{instance_id}: the arms disagree on the bare score "
                f"({arm_a_dir.name}={a['score']}, {arm_b_dir.name}={b['score']}); "
                "they did not refine the same starting query, so no delta is attributable"
            )
        db = db_of.get(instance_id, "")
        instance_dir = dirs_b.get(instance_id)
        result.rows.append(Row(
            paired=a["score"] == b["score"],
            instance_id=instance_id,
            db=db,
            score_bare=b["score"],
            score_arm_a=a["score_final"],
            score_arm_b=b["score_final"],
            delta_knowledge=b["score_final"] - a["score_final"],
            delta_paper=b["score_final"] - b["score"],
            rules_used=rules_used(instance_dir) if instance_dir else [],
            n_db_rules=counts.get(db.lower(), 0),
        ))
    return result


def summarise_by_db_rules(rows: List[Row]) -> List[GroupSummary]:
    """Overall, then split on whether any db-scoped rule was even available.

    Without the split, an overall gain near zero cannot be told apart from most
    instances having had no knowledge to draw on in the first place.

    Unpaired rows are excluded from every group and counted separately: their delta
    measures something other than knowledge, and averaging them in would corrupt the
    headline number behind a warning line.
    """
    def group_of(candidates: List[Row]) -> List[Row]:
        return [r for r in candidates if r.paired]

    groups = [
        ("overall", rows),
        ("no db rules", [r for r in rows if r.n_db_rules == 0]),
        ("has db rules", [r for r in rows if r.n_db_rules > 0]),
    ]
    return [
        GroupSummary(
            label=label,
            n=len(group_of(candidates)),
            bare=sum(r.score_bare for r in group_of(candidates)),
            arm_a=sum(r.score_arm_a for r in group_of(candidates)),
            arm_b=sum(r.score_arm_b for r in group_of(candidates)),
            improved=sum(1 for r in group_of(candidates) if r.delta_knowledge > 0),
            regressed=sum(1 for r in group_of(candidates) if r.delta_knowledge < 0),
            unpaired=sum(1 for r in candidates if not r.paired),
        )
        for label, candidates in groups
    ]
