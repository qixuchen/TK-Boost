"""Deterministic gates on the reflector's divergences (TK-Boost-adapt.md 5.5, 5.6).

Only ``KIND: data`` divergences are gated; ``non_data`` and ``gold_suspect`` are
logged and produce no rule. A data divergence carries its rule fields and is
accepted only if every check passes; its reasons are sent back to the reflector
otherwise.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from .catalog import Catalog
from .milestones import MATCH, MISMATCH, SEMANTIC, UNVERIFIABLE, compare, find_milestone
from .reflector_io import Divergence, Evidence, Reproduction
from .scope import check_body, check_scope_consistency, validate_refs

ACCEPTED = "accepted"
REJECTED = "rejected"
LOGGED = "logged"
BASES = ("data", "task", "gold_only")

_NUMBER = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?")


@dataclass
class GateResult:
    divergence: Divergence
    status: str
    reasons: list[str] = field(default_factory=list)


def missed_milestones(process_score: dict[str, Any] | None) -> set[str]:
    """Keys of the milestones the process judge marked as not achieved."""
    details = ((process_score or {}).get("gpr") or {}).get("details") or []
    return {item["key"] for item in details if not item.get("achieved")}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _numbers_in(text: str) -> list[float]:
    numbers = []
    for token in _NUMBER.findall(text):
        try:
            numbers.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return numbers


def _scalars(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _scalars(item)
    elif value is not None:
        yield value


def _missing_from_output(value: Any, output: str) -> list[str]:
    squashed = _squash(output)
    numbers = None
    missing = []
    for scalar in _scalars(value):
        if isinstance(scalar, bool):
            found = str(scalar).lower() in squashed.lower()
        elif isinstance(scalar, (int, float)):
            numbers = _numbers_in(output) if numbers is None else numbers
            found = any(math.isclose(n, scalar, rel_tol=1e-9, abs_tol=1e-12) for n in numbers)
        else:
            found = _squash(str(scalar)) in squashed
        if not found:
            missing.append(str(scalar))
    return missing


def _check_reproduction(
    r: Reproduction,
    probes: Mapping[int, str],
    milestones: dict[str, Any],
    missed: set[str],
) -> list[str]:
    reasons: list[str] = []
    found = find_milestone(milestones, r.key)
    if found is None:
        reasons.append(
            f'milestone "{r.key}" is not a gold milestone key; '
            f"use one of the missed milestones: {', '.join(sorted(missed))}"
        )
        return reasons
    key, expected = found
    if key not in missed:
        reasons.append(
            f'milestone "{key}" was already achieved by the agent; reproduce a milestone it missed'
        )
    output = probes.get(r.probe)
    if output is None:
        reasons.append(f'REPRODUCED "{key}" cites probe#{r.probe}, which was never run')
    else:
        missing = _missing_from_output(r.value, output)
        if missing:
            reasons.append(
                f'REPRODUCED "{key}": {", ".join(missing)} does not appear in the output of '
                f"probe#{r.probe}; copy the value exactly as the probe printed it"
            )
    verdict = compare(expected, r.value)
    if verdict == MISMATCH:
        reasons.append(f'REPRODUCED "{key}" = {r.value!r} does not match gold {expected!r}')
    elif verdict == UNVERIFIABLE:
        reasons.append(f'milestone "{key}" cannot be verified by the harness; reproduce another one')
    elif verdict == SEMANTIC and not (r.semantic_match or "").strip():
        reasons.append(
            f'REPRODUCED "{key}" = {r.value!r} differs in wording from gold {expected!r}; '
            "add a SEMANTIC_MATCH line explaining why they mean the same thing"
        )
    return reasons


def check_divergence(
    d: Divergence,
    *,
    probes: Mapping[int, str],
    catalog: Catalog,
    milestones: dict[str, Any],
    missed: set[str],
    task_prompt: str,
) -> GateResult:
    if d.kind in ("non_data", "gold_suspect"):
        return GateResult(d, LOGGED, list(d.errors))
    reasons = list(d.errors)
    for name, value in (("SCOPE", d.scope), ("FACT", d.fact)):
        if not value.strip():
            reasons.append(f"{name} is missing")
    if not d.evidence:
        reasons.append("EVIDENCE is missing; cite a probe whose output shows the fact")
    if not d.reproduced:
        reasons.append("REPRODUCED is missing; reproduce at least one missed milestone")

    reasons.extend(_check_excerpts("EVIDENCE", d.evidence, probes))

    reasons.extend(validate_refs(d.tables, d.columns, catalog))
    if d.scope.strip():
        scope_error = check_scope_consistency(d.scope, d.tables, d.columns)
        if scope_error:
            reasons.append(scope_error)

    for r in d.reproduced:
        reasons.extend(_check_reproduction(r, probes, milestones, missed))

    reasons.extend(_check_rule_fields(d, catalog))
    reasons.extend(_check_basis(d, task_prompt))
    if not d.generality:
        reasons.append(
            "GENERALITY is missing; cite a probe showing the fact holds beyond the entities of this task"
        )
    reasons.extend(_check_excerpts("GENERALITY", d.generality, probes))

    return GateResult(d, REJECTED if reasons else ACCEPTED, reasons)


def _check_excerpts(name: str, excerpts: list[Evidence], probes: Mapping[int, str]) -> list[str]:
    reasons = []
    for e in excerpts:
        output = probes.get(e.probe)
        if output is None:
            reasons.append(f"{name} cites probe#{e.probe}, which was never run")
        elif not e.excerpt.strip() or _squash(e.excerpt) not in _squash(output):
            reasons.append(
                f"{name} excerpt {e.excerpt!r} not found in the output of "
                f"probe#{e.probe}; quote the output verbatim"
            )
    return reasons


def _check_rule_fields(d: Divergence, catalog: Catalog) -> list[str]:
    reasons = [
        f"{name} is missing"
        for name, value in (
            ("NEEDED", d.needed),
            ("INSTANCE", d.instance),
            ("ENSURE", d.ensure),
            ("WHEN_TO_CHECK", d.when_to_check),
            ("CONTEXT", d.context),
            ("EXAMPLE_USAGE", d.example_usage),
        )
        if not value.strip()
    ]
    reasons.extend(check_body(
        {"ensure": d.ensure, "when_to_check": d.when_to_check, "context": d.context}, catalog
    ))
    return reasons


def _check_basis(d: Divergence, task_prompt: str) -> list[str]:
    if not d.basis:
        return [f"BASIS is missing; use one of {', '.join(BASES)}"]
    if d.basis not in BASES:
        return [f"BASIS {d.basis!r} is not one of {', '.join(BASES)}"]
    if d.basis != "task":
        return []
    if not d.basis_quote.strip():
        return ["BASIS_QUOTE is missing; BASIS: task needs the words of the task that decide it"]
    if _squash(d.basis_quote).lower() not in _squash(task_prompt).lower():
        return [
            f"BASIS_QUOTE {d.basis_quote!r} does not appear in the task; "
            "copy the words verbatim from the task"
        ]
    return []
