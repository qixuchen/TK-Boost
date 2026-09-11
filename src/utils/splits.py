"""Reproducible train/test splits over benchmark instance ids.

Populate reads gold SQL and gold results, so it sees the answers. The split must
therefore be fixed before populate runs: reusing a populated instance at
evaluation time would leak.

The paper's Table 11 reports only split sizes, not instance ids, so the split is
generated here from a recorded seed rather than reproduced from the paper.
"""

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
