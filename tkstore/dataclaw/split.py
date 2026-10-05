"""Stratified train/test split over DataClaw tasks.

Tasks are stratified by ``(category, level)``. Half of each stratum goes to test
and half to train, and train is halved the same way into ``train_a`` (run first)
and ``train_b`` (run only if ``train_a`` yields too few trajectories).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from .devset import parse_task_markdown

LEVELS = ("easy", "medium", "hard")

Stratum = tuple[str, str]


@dataclass(frozen=True)
class TaskSplit:
    test: list[str]
    train_a: list[str]
    train_b: list[str]

    @property
    def train(self) -> list[str]:
        return sorted(self.train_a + self.train_b)


def task_strata(tasks_dir: Path) -> dict[str, Stratum]:
    """Map each ``task_*.md`` id to ``(category, level)``; level comes from ``name``."""
    strata: dict[str, Stratum] = {}
    for path in sorted(Path(tasks_dir).glob("task_*.md")):
        meta, _ = parse_task_markdown(path)
        category, level, _ = str(meta["name"]).rsplit("-", 2)
        if category != meta["category"]:
            raise ValueError(f"{path.name}: name {meta['name']!r} disagrees with category {meta['category']!r}")
        if level not in LEVELS:
            raise ValueError(f"{path.name}: unknown level {level!r} in name {meta['name']!r}")
        strata[str(meta["id"])] = (category, level)
    return strata


def stratified_halves(strata: dict[str, Stratum], seed: int) -> tuple[list[str], list[str]]:
    """Halve every stratum; odd strata give their extra task to the two sides in turn."""
    groups: dict[Stratum, list[str]] = {}
    for task_id in sorted(strata):
        groups.setdefault(strata[task_id], []).append(task_id)

    rng = random.Random(seed)
    first: list[str] = []
    second: list[str] = []
    extra_to_first = True
    for stratum in sorted(groups):
        ids = groups[stratum]
        rng.shuffle(ids)
        cut = len(ids) // 2
        if len(ids) % 2:
            cut += extra_to_first
            extra_to_first = not extra_to_first
        first.extend(ids[:cut])
        second.extend(ids[cut:])
    return sorted(first), sorted(second)


def split_tasks(strata: dict[str, Stratum], seed: int) -> TaskSplit:
    test, train = stratified_halves(strata, seed)
    train_a, train_b = stratified_halves({t: strata[t] for t in train}, seed)
    return TaskSplit(test=test, train_a=train_a, train_b=train_b)
