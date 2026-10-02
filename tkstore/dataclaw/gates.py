"""Deterministic gates on the reflector's divergences (TK-Boost-adapt.md 5.5–5.7).

Only ``KIND: data`` divergences are gated; ``non_data`` and ``gold_suspect`` are
logged and produce no rule. The structural check comes first: parse errors,
missing fields, pointers that do not resolve to lines the reflector was shown,
unknown files, columns or milestone keys. Resolved pointers get the cited lines
filled in. A divergence that is structurally sound then goes through the
substantive check against gold. Only substantive rejections use up a
``<final>`` submission, so ``GateResult.structural`` tells the two apart.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from .catalog import Catalog
from .milestones import MISMATCH, UNVERIFIABLE, compare, find_milestone
from .probe import ProbeLines
from .reflector_io import Divergence, Evidence, Reproduction
from .scope import validate_refs

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
    structural: bool = False


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


def _span(start: int, end: int) -> str:
    return f"L{start}" if start == end else f"L{start}-L{end}"


def _resolve(name: str, pointer: Evidence | Reproduction, probes: Mapping[int, ProbeLines]) -> str | None:
    """Fill ``pointer.excerpt`` with the cited lines; return the reason when they cannot be cited."""
    cited = f"P{pointer.probe}:{_span(pointer.start, pointer.end)}"
    view = probes.get(pointer.probe)
    if view is None:
        ran = f"probes run so far: {', '.join(f'P{n}' for n in sorted(probes))}" if probes else (
            "no probe has been run yet"
        )
        return (
            f"{name} cites P{pointer.probe}, which was never run; {ran}. P<n> numbers your own "
            "<probe> runs, not the agent's CALL #n; run a probe before citing its output"
        )
    if pointer.end > len(view.lines):
        return f"{name} {cited} is outside the output of P{pointer.probe}, which has {len(view.lines)} line(s)"
    if any(n not in view.visible for n in range(pointer.start, pointer.end + 1)):
        return (
            f"{name} {cited} points at lines omitted from the output you were shown; cite lines you "
            "can see, or print the ones you need with a narrower probe"
        )
    pointer.excerpt = "\n".join(view.lines[pointer.start - 1 : pointer.end])
    return None


def _resolve_all(name: str, pointers: list, probes: Mapping[int, ProbeLines]) -> list[str]:
    return [reason for p in pointers if (reason := _resolve(name, p, probes))]


def _reproduction_form(r: Reproduction, probes: Mapping[int, ProbeLines], milestones: dict[str, Any],
                       missed: set[str]) -> list[str]:
    found = find_milestone(milestones, r.key)
    if found is None:
        return [
            f'milestone "{r.key}" is not a gold milestone key; '
            f"use one of the missed milestones: {', '.join(sorted(missed))}"
        ]
    key, _ = found
    reason = _resolve("REPRODUCED", r, probes)
    if reason:
        return [reason]
    reasons = []
    missing = _missing_from_output(r.value, r.excerpt)
    if missing:
        reasons.append(
            f'REPRODUCED "{key}": {", ".join(missing)} does not appear in '
            f"P{r.probe}:{_span(r.start, r.end)}; point at the line that prints the value"
        )
    return reasons


def _reproduction_substance(r: Reproduction, milestones: dict[str, Any], missed: set[str]) -> list[str]:
    key, expected = find_milestone(milestones, r.key)
    reasons = []
    if key not in missed:
        reasons.append(
            f'milestone "{key}" was already achieved by the agent; reproduce a milestone it missed'
        )
    verdict = compare(expected, r.value)
    if verdict == MISMATCH:
        reasons.append(f'REPRODUCED "{key}" = {r.value!r} does not match gold {expected!r}')
    elif verdict == UNVERIFIABLE:
        reasons.append(f'milestone "{key}" cannot be verified by the harness; reproduce another one')
    return reasons


def check_structure(
    d: Divergence,
    *,
    probes: Mapping[int, ProbeLines],
    catalog: Catalog,
    milestones: dict[str, Any],
    missed: set[str],
    task_prompt: str,
) -> list[str]:
    """Reasons a data divergence is malformed; resolved pointers get their lines filled in."""
    reasons = list(d.errors)
    if not d.fact.strip():
        reasons.append("FACT is missing")
    if not d.evidence:
        reasons.append("EVIDENCE is missing; cite a probe whose output shows the fact")
    if not d.reproduced:
        reasons.append("REPRODUCED is missing; reproduce at least one missed milestone")
    reasons.extend(_resolve_all("EVIDENCE", d.evidence, probes))
    reasons.extend(validate_refs(d.tables, d.columns, catalog))
    for r in d.reproduced:
        reasons.extend(_reproduction_form(r, probes, milestones, missed))
    reasons.extend(_check_rule_fields(d))
    reasons.extend(_check_basis(d, task_prompt))
    if not d.generality:
        reasons.append(
            "GENERALITY is missing; cite a probe showing the fact holds beyond the entities of this task"
        )
    reasons.extend(_resolve_all("GENERALITY", d.generality, probes))
    return reasons


def check_substance(d: Divergence, *, milestones: dict[str, Any], missed: set[str]) -> list[str]:
    """Reasons a structurally sound divergence does not reproduce what the agent missed."""
    return [reason for r in d.reproduced for reason in _reproduction_substance(r, milestones, missed)]


def check_divergence(
    d: Divergence,
    *,
    probes: Mapping[int, ProbeLines],
    catalog: Catalog,
    milestones: dict[str, Any],
    missed: set[str],
    task_prompt: str,
) -> GateResult:
    if d.kind in ("non_data", "gold_suspect"):
        return GateResult(d, LOGGED, list(d.errors))
    reasons = check_structure(
        d, probes=probes, catalog=catalog, milestones=milestones, missed=missed, task_prompt=task_prompt
    )
    if reasons:
        return GateResult(d, REJECTED, reasons, structural=True)
    reasons = check_substance(d, milestones=milestones, missed=missed)
    return GateResult(d, REJECTED if reasons else ACCEPTED, reasons)


def _check_rule_fields(d: Divergence) -> list[str]:
    return [
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
