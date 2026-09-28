"""Compare a reproduced value against a gold milestone.

Numbers are judged here with DataClaw's process-grading tolerance
(``dataclaw/utils/process_grading.py``: 1% relative, 1e-6 absolute near zero).
Strings only match here when equal after whitespace/case normalization; gold is
English while the data is Chinese, so anything else is returned as ``SEMANTIC``
for the reflector to judge. Dict milestones must be reported under gold's keys,
so that their values stay harness-checked.
"""

from __future__ import annotations

import math
import re
from typing import Any

MATCH = "match"
MISMATCH = "mismatch"
SEMANTIC = "semantic"
UNVERIFIABLE = "unverifiable"

NUMERIC_REL_TOL = 0.01
NUMERIC_ABS_TOL = 1e-6

_SEVERITY = {MATCH: 0, SEMANTIC: 1, UNVERIFIABLE: 2, MISMATCH: 3}


def _numbers_match(expected: float, actual: float) -> bool:
    if math.isnan(expected) or math.isnan(actual):
        return False
    if expected == actual:
        return True
    if abs(expected) < NUMERIC_ABS_TOL:
        return abs(actual - expected) < NUMERIC_ABS_TOL
    return abs(actual - expected) / abs(expected) <= NUMERIC_REL_TOL


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _worst(verdicts: list[str]) -> str:
    return max(verdicts, key=_SEVERITY.__getitem__, default=MATCH)


def _compare_bool(expected: bool, reproduced: Any) -> str:
    if isinstance(reproduced, bool):
        return MATCH if reproduced == expected else MISMATCH
    if isinstance(reproduced, str) and _norm_text(reproduced) in ("true", "false"):
        return MATCH if (_norm_text(reproduced) == "true") == expected else MISMATCH
    return MISMATCH


def _compare_list(expected: list, reproduced: Any) -> str:
    if not isinstance(reproduced, list):
        return MISMATCH
    if any(isinstance(item, (list, dict)) for item in expected + reproduced):
        return UNVERIFIABLE

    def dedupe(items: list) -> list:
        seen: dict[Any, Any] = {}
        for item in items:
            number = _as_number(item)
            seen.setdefault(("n", number) if number is not None else ("s", _norm_text(item)), item)
        return list(seen)

    want, got = dedupe(expected), dedupe(reproduced)
    if len(want) != len(got):
        return MISMATCH

    remaining = list(got)
    unmatched_strings = 0
    for kind, value in want:
        if kind == "n":
            hit = next(
                (g for g in remaining if g[0] == "n" and _numbers_match(value, g[1])), None
            )
            if hit is None:
                return MISMATCH
            remaining.remove(hit)
        elif ("s", value) in remaining:
            remaining.remove(("s", value))
        else:
            unmatched_strings += 1

    if unmatched_strings == 0:
        return MATCH
    leftover_strings = sum(1 for g in remaining if g[0] == "s")
    return SEMANTIC if leftover_strings == unmatched_strings == len(remaining) else MISMATCH


def _compare_dict(expected: dict, reproduced: Any) -> str:
    if not isinstance(reproduced, dict):
        return MISMATCH
    got = {_norm_text(key): value for key, value in reproduced.items()}
    if set(got) != {_norm_text(key) for key in expected}:
        return MISMATCH
    return _worst([compare(value, got[_norm_text(key)]) for key, value in expected.items()])


def compare(expected: Any, reproduced: Any) -> str:
    """Return MATCH, MISMATCH, SEMANTIC (reflector judges) or UNVERIFIABLE."""
    if expected is None:
        return UNVERIFIABLE
    if isinstance(expected, bool):
        return _compare_bool(expected, reproduced)
    if isinstance(expected, list):
        return _compare_list(expected, reproduced)
    if isinstance(expected, dict):
        return _compare_dict(expected, reproduced)

    want = _as_number(expected)
    if want is not None:
        got = _as_number(reproduced)
        return MATCH if got is not None and _numbers_match(want, got) else MISMATCH

    if not isinstance(reproduced, str):
        return MISMATCH
    return MATCH if _norm_text(expected) == _norm_text(reproduced) else SEMANTIC


def find_milestone(milestones: dict[str, Any], key: str) -> tuple[str, Any] | None:
    """Look up a gold milestone by key, tolerating only whitespace differences."""
    wanted = re.sub(r"\s+", " ", key).strip()
    for gold_key, value in milestones.items():
        if re.sub(r"\s+", " ", gold_key).strip() == wanted:
            return gold_key, value
    return None
