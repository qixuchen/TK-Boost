"""Parse the reflector's turns: one ``<probe>`` or one ``<final>``.

A ``<final>`` holds divergence blocks, each starting with ``DIVERGENCE:``, in
the format of TK-Boost-adapt.md section 5.4. Structural mistakes are collected
per block in ``Divergence.errors`` so they can be sent back to the reflector.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .scope import ColumnRef, parse_columns, parse_tables

KINDS = ("data", "non_data", "gold_suspect")
_FIELDS = (
    "DIVERGENCE", "NEEDED", "MISSING_DATA_UNDERSTANDING", "SCOPE", "TABLES", "COLUMNS",
    "FACT", "CATEGORY", "EVIDENCE", "REPRODUCED", "SEMANTIC_MATCH", "KIND",
)
_FIELD_LINE = re.compile(rf"^\s*({'|'.join(_FIELDS)}):\s?(.*)$")
_EVIDENCE = re.compile(r"^probe#(\d+)\s*(?:→|->|:)\s*(.*)$", re.DOTALL)
_REPRODUCED = re.compile(r'^milestone\s+"(.*)"\s*=\s*(.*?)\s+FROM\s+probe#(\d+)\s*$', re.DOTALL)
_PROBE = re.compile(r"<probe>(.*?)</probe>", re.DOTALL)
_FINAL = re.compile(r"<final>(.*?)(?:</final>|$)", re.DOTALL)


@dataclass
class Evidence:
    probe: int
    excerpt: str


@dataclass
class Reproduction:
    key: str
    value: Any
    probe: int
    semantic_match: str | None = None


@dataclass
class Divergence:
    index: int
    divergence: str = ""
    needed: str = ""
    scope: str = ""
    tables: list[str] = field(default_factory=list)
    columns: list[ColumnRef] = field(default_factory=list)
    fact: str = ""
    category: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    reproduced: list[Reproduction] = field(default_factory=list)
    kind: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class Turn:
    kind: str
    plan: str = ""
    command: str = ""
    divergences: list[Divergence] = field(default_factory=list)
    note: str = ""
    error: str = ""


def _strip_fence(command: str) -> str:
    lines = command.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _field_entries(block: str) -> list[tuple[str, str]]:
    entries: list[list[str]] = []
    for line in block.splitlines():
        match = _FIELD_LINE.match(line)
        if match:
            entries.append([match.group(1), match.group(2).strip()])
        elif entries and line.strip():
            entries[-1][1] = f"{entries[-1][1]}\n{line.strip()}".strip()
    return [(key, value) for key, value in entries]


def _parse_block(index: int, block: str) -> Divergence:
    d = Divergence(index=index)
    for key, value in _field_entries(block):
        if key == "DIVERGENCE":
            d.divergence = value
        elif key == "NEEDED":
            d.needed = value
        elif key == "SCOPE":
            d.scope = value
        elif key == "FACT":
            d.fact = value
        elif key == "CATEGORY":
            d.category = value
        elif key in ("TABLES", "COLUMNS"):
            try:
                if key == "TABLES":
                    d.tables = parse_tables(value)
                else:
                    d.columns = parse_columns(value)
            except ValueError as exc:
                d.errors.append(str(exc))
        elif key == "EVIDENCE":
            match = _EVIDENCE.match(value)
            if match:
                d.evidence.append(Evidence(int(match.group(1)), match.group(2).strip()))
            else:
                d.errors.append(f"EVIDENCE must be written as probe#<n> → <excerpt>, got {value!r}")
        elif key == "REPRODUCED":
            match = _REPRODUCED.match(value)
            if not match:
                d.errors.append(
                    'REPRODUCED must be written as milestone "<key>" = <JSON value> FROM probe#<m>, '
                    f"got {value!r}"
                )
                continue
            try:
                parsed = json.loads(match.group(2))
            except json.JSONDecodeError:
                d.errors.append(
                    f"REPRODUCED value {match.group(2)!r} is not valid JSON; quote strings, e.g. \"广东省\""
                )
                continue
            d.reproduced.append(Reproduction(match.group(1).strip(), parsed, int(match.group(3))))
        elif key == "SEMANTIC_MATCH":
            if d.reproduced:
                d.reproduced[-1].semantic_match = value
            else:
                d.errors.append("SEMANTIC_MATCH must follow the REPRODUCED line it justifies")
        elif key == "KIND":
            d.kind = value.strip().lower()
    if not d.kind:
        d.errors.append(f"KIND is missing; use one of {', '.join(KINDS)}")
    elif d.kind not in KINDS:
        d.errors.append(f"KIND {d.kind!r} is not one of {', '.join(KINDS)}")
    return d


def _parse_final(body: str) -> Turn:
    note_match = re.search(r"NO_DATA_DIVERGENCE:\s*(.*)", body)
    starts = [m.start() for m in re.finditer(r"^\s*DIVERGENCE:", body, re.MULTILINE)]
    blocks = [body[s:e] for s, e in zip(starts, starts[1:] + [len(body)])]
    return Turn(
        kind="final",
        divergences=[_parse_block(i + 1, b) for i, b in enumerate(blocks)],
        note=note_match.group(1).strip() if note_match else "",
    )


def parse_turn(text: str) -> Turn:
    probes = _PROBE.findall(text)
    final = _FINAL.search(text)
    if probes and final:
        return Turn(kind="invalid", error="turn has both a <probe> and a <final>; send exactly one")
    if final:
        return _parse_final(final.group(1))
    if not probes:
        return Turn(kind="invalid", error="turn has neither a <probe> nor a <final>")
    if len(probes) > 1:
        return Turn(kind="invalid", error="send exactly one <probe> per turn")
    command = _strip_fence(probes[0])
    if not command:
        return Turn(kind="invalid", error="<probe> is empty")
    plan = re.search(r"PLAN:\s*(.*?)(?=<probe>|$)", text, re.DOTALL)
    return Turn(kind="probe", plan=plan.group(1).strip() if plan else "", command=command)
