"""Reproducible train/test splits over benchmark instance ids.

Populate reads gold SQL and gold results, so it sees the answers. The split must
therefore be fixed before populate runs: reusing a populated instance at
evaluation time would leak.

The split is driven by data availability rather than a seed. Algorithm 3 needs the
gold SQL ``s*``, but Spider2-lite publishes it for only 24 of the 135 SQLite
instances while publishing gold *results* for all 135. So the instances that have
gold SQL become train and the rest become test, which is what
`partition_by_gold_sql` does. `make_split` is kept for the seeded random case.
"""

import csv
import random
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


def _validated_population(instance_ids: Sequence[str]) -> List[str]:
    ids = list(instance_ids)
    if not ids:
        raise ValueError("instance_ids is empty; nothing to split.")
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"instance_ids contains duplicates: {duplicates}")
    return sorted(ids)


def make_split(
    instance_ids: Sequence[str],
    train_size: int,
    seed: int,
) -> Tuple[List[str], List[str]]:
    """Split ids into ``(train, test)``.

    Sorting the population first makes the result independent of input order, and
    both returned lists are sorted so the generated files diff cleanly.
    """
    population = _validated_population(instance_ids)
    if not 0 <= train_size <= len(population):
        raise ValueError(
            f"train_size must be between 0 and {len(population)}, got {train_size}."
        )

    train = random.Random(seed).sample(population, train_size)
    train_set = set(train)
    return sorted(train), [i for i in population if i not in train_set]


def partition_by_gold_sql(
    instance_ids: Sequence[str],
    gold_sql_dir: Path,
) -> Tuple[List[str], List[str]]:
    """Split ids into ``(train, test)`` by whether ``<gold_sql_dir>/<id>.sql`` exists.

    A missing directory raises rather than yielding an empty train set, since a
    typo'd path would otherwise look like "no instance has gold SQL".
    """
    gold_sql_dir = Path(gold_sql_dir)
    if not gold_sql_dir.is_dir():
        raise FileNotFoundError(f"Gold SQL directory not found: {gold_sql_dir}")

    population = _validated_population(instance_ids)
    train = [i for i in population if (gold_sql_dir / f"{i}.sql").is_file()]
    train_set = set(train)
    return train, [i for i in population if i not in train_set]


def load_store_instance_ids(store_path: Path) -> List[str]:
    """Distinct instance ids appearing in a TK-Store CSV, sorted.

    These are the instances a store was trained on, so they must be kept out of any
    test set that store is evaluated against.
    """
    store_path = Path(store_path)
    if not store_path.is_file():
        raise FileNotFoundError(f"TK-Store CSV not found: {store_path}")

    with store_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "instance_id" not in reader.fieldnames:
            raise ValueError(
                f"{store_path} has no 'instance_id' column; got {reader.fieldnames}"
            )
        ids = {(row.get("instance_id") or "").strip() for row in reader}
    return sorted(i for i in ids if i)


def uncontaminated_subset(
    test_ids: Sequence[str],
    reference_store_ids: Iterable[str],
) -> List[str]:
    """Test ids a reference store was not trained on.

    Reference ids that never appear in the test set are ignored, so passing a
    store built over a different split is harmless.
    """
    population = _validated_population(test_ids)
    excluded = set(reference_store_ids)
    return [i for i in population if i not in excluded]


def write_split(path: Path, instance_ids: Iterable[str], header: Optional[str] = None) -> Path:
    """Write one instance id per line, optionally preceded by a ``#`` comment."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if header:
        lines.extend(f"# {line}" for line in header.splitlines())
    lines.extend(instance_ids)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_split(path: Path) -> List[str]:
    """Read a split file, skipping blank lines and ``#`` comments."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Split file not found: {path}")

    ids: List[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)

    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"{path} contains duplicate instance ids: {duplicates}")
    return ids
