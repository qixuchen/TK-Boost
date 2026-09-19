"""Planning the three-arm comparison of stage 4.

The whole comparison rests on one invariant: both refinement arms start from the
*same* agent output, so the only difference between them is the tribal-knowledge
store. Two consequences shape this module.

Only the agent step is batched. `--refine-output` walks the directory it is given
and ignores instance selection, so passing `--split` to an arm would suggest a
filter that does not exist.

`verify_shared_agent_output` checks the invariant after the fact rather than trusting
it: a mismatch means `delta_knowledge` is not a paired comparison and the numbers
cannot be read as knowledge gain.
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from src.utils.splits import load_split

PYTHON = sys.executable
RUNNER_MODULE = "src.agents.sql_agent_runner"
STARTING_SQL = "execution_query.sql"


@dataclass(frozen=True)
class Step:
    """One runner invocation."""

    name: str
    argv: List[str]


@dataclass(frozen=True)
class Batch:
    """A slice of the split, with the three directories its arms live in."""

    offset: int
    limit: Optional[int]
    agent_dir: Path
    refonly_dir: Path
    tk_dir: Path


def plan_batches(
    split_path: Path,
    out_prefix: Path,
    batch_size: Optional[int] = None,
) -> List[Batch]:
    """Slice `split_path` into batches, each with its own set of directories.

    A batch bounds the wall-clock time of a single command, not the damage of an
    interruption: both runner paths resume per instance (see C19 / C20).

    The last batch carries its real size rather than the nominal `batch_size`, so a
    logged command reproduces exactly that batch on its own.
    """
    total = len(load_split(Path(split_path)))
    out_prefix = Path(out_prefix)

    if batch_size is None:
        return [_batch(0, None, out_prefix, batched=False)]
    if batch_size < 1:
        raise ValueError(f"batch_size must be positive: {batch_size}")

    offsets = range(0, total, batch_size)
    batched = len(offsets) > 1
    return [
        _batch(off, min(batch_size, total - off), out_prefix, batched=batched)
        for off in offsets
    ]


def _batch(offset: int, limit: Optional[int], out_prefix: Path, batched: bool) -> Batch:
    stem = f"{out_prefix.name}_b{offset}" if batched else out_prefix.name
    parent = out_prefix.parent
    return Batch(
        offset=offset,
        limit=limit,
        agent_dir=parent / f"{stem}_agent",
        refonly_dir=parent / f"{stem}_refonly",
        tk_dir=parent / f"{stem}_tk",
    )


def plan_steps(
    batch: Batch,
    split_path: Path,
    tkstore: str,
    model: Optional[str] = None,
    filter_model: Optional[str] = None,
    use_llm_filtering: bool = True,
    refiner_turns: Optional[int] = None,
    refiner_min_probes: Optional[int] = None,
    adopt_refiner_sql: bool = False,
) -> List[Step]:
    """The three invocations for one batch, in the order they must run.

    The agent step deliberately carries no refinement flag: its `execution_query.sql`
    is the bare-agent number, which both arms then start from. For the same reason the
    refiner budget is only passed to the arms, and only when asked -- omitting it leaves
    the runner's own default, which is what the reference run was measured with.
    """
    # `-u` because the child prints the progress: piped to a log it would otherwise
    # block-buffer and look hung for kilobytes at a time.
    base = [PYTHON, "-u", "-m", RUNNER_MODULE]
    common = ["--model", model] if model else []

    agent = base + ["--split", str(split_path), "--out-base", str(batch.agent_dir)] + common
    if batch.offset:
        agent += ["--split-offset", str(batch.offset)]
    if batch.limit is not None:
        agent += ["--split-limit", str(batch.limit)]

    budget: List[str] = []
    if refiner_turns is not None:
        budget += ["--refiner-turns", str(refiner_turns)]
    if refiner_min_probes is not None:
        budget += ["--refiner-min-probes", str(refiner_min_probes)]
    # Both arms or neither: the flag changes how a verdict is applied, so a one-sided
    # setting would make the pairing measure adoption rather than knowledge.
    if adopt_refiner_sql:
        budget += ["--adopt-refiner-sql"]

    refonly = base + [
        "--refine-output", str(batch.agent_dir),
        "--refine-output-dir", str(batch.refonly_dir),
    ] + common + budget

    tk = base + [
        "--refine-output", str(batch.agent_dir),
        "--refine-output-dir", str(batch.tk_dir),
        "--tkstore", str(tkstore),
    ] + common + budget
    if filter_model:
        tk += ["--filter-model", filter_model]
    if not use_llm_filtering:
        tk += ["--no-llm-filtering"]

    return [
        Step(name="agent", argv=agent),
        Step(name="arm_refonly", argv=refonly),
        Step(name="arm_tk", argv=tk),
    ]


def _instance_dirs(base: Path) -> List[Path]:
    return sorted(d for d in base.iterdir() if d.is_dir() and not d.name.startswith('.'))


def verify_shared_agent_output(agent_dir: Path, arm_dir: Path) -> List[str]:
    """Instance directories whose starting SQL differs between the shared run and an arm.

    Refinement writes its revisions to `execution_query_after_*.sql` and leaves the
    original alone, so any difference here means the arms did not start from the same
    query and the comparison is not paired.

    Instances the agent never finished carry no starting SQL and are not synced into the
    arms, so they are ignored rather than reported -- failing the pipeline over an
    instance nobody could have refined would hide the arms that did run.
    """
    agent_dir, arm_dir = Path(agent_dir), Path(arm_dir)
    for d in (agent_dir, arm_dir):
        if not d.is_dir():
            raise FileNotFoundError(f"Directory not found: {d}")

    def starting_sql(base: Path, name: str) -> Optional[bytes]:
        path = base / name / STARTING_SQL
        if not path.is_file():
            return None
        raw = path.read_bytes()
        return raw if raw.strip() else None

    names = {d.name for d in _instance_dirs(agent_dir)} | {d.name for d in _instance_dirs(arm_dir)}
    return sorted(
        n for n in names
        if starting_sql(agent_dir, n) is not None
        and starting_sql(agent_dir, n) != starting_sql(arm_dir, n)
    )


def _subprocess_run(argv: Sequence[str]) -> int:
    return subprocess.call(list(argv))


def run_steps(
    steps: Sequence[Step],
    run: Callable[[Sequence[str]], int] = _subprocess_run,
    dry_run: bool = False,
) -> int:
    """Run `steps` in order, stopping at the first failure.

    Continuing past a failed agent step would refine a half-finished directory and
    silently shrink the sample, which looks like a result rather than an error.
    """
    for step in steps:
        print(f"\n▶️  {step.name}: {' '.join(step.argv)}")
        if dry_run:
            continue
        code = run(step.argv)
        if code != 0:
            print(f"❌ {step.name} exited {code}; stopping")
            return code
    return 0
