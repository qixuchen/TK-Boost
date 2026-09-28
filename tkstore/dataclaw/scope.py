"""COLUMNS parsing and SCOPE derivation shared by divergences and rules.

A COLUMNS entry is ``<file>.<column>``, where ``<file>`` is a path relative to
DataClaw's ``database/`` directory ending in ``.csv`` or ``.json``; a whole file
is written ``<file>.all``. An empty COLUMNS list means a generic rule.
"""

from __future__ import annotations

import re
from typing import Mapping, NamedTuple

from .catalog import Catalog

SCOPES = ("column", "multi_column", "file", "cross_table", "generic")
WHOLE_FILE = "all"
BODY_FIELDS = ("ensure", "when_to_check", "context")

_ENTRY = re.compile(r"^(?P<file>.+?\.(?:csv|json))\.(?P<column>.+)$")
_DATABASE_PREFIX = re.compile(r"^(?:\./)?database/")


class ColumnRef(NamedTuple):
    file: str
    column: str


def parse_columns(text: str) -> list[ColumnRef]:
    refs: list[ColumnRef] = []
    for raw in re.split(r"[,\n]", text or ""):
        entry = _DATABASE_PREFIX.sub("", raw.strip())
        if not entry:
            continue
        match = _ENTRY.match(entry)
        if not match or not match.group("column").strip():
            raise ValueError(
                f"COLUMNS entry {raw.strip()!r} is not <file>.<column> or <file>.all"
            )
        ref = ColumnRef(match.group("file"), match.group("column").strip())
        if ref not in refs:
            refs.append(ref)
    return refs


def derive_scope(refs: list[ColumnRef]) -> str:
    if not refs:
        return "generic"
    files = {ref.file for ref in refs}
    if len(files) >= 2:
        return "cross_table"
    columns = {ref.column for ref in refs}
    if WHOLE_FILE in columns:
        if len(columns) > 1:
            raise ValueError(
                f"{refs[0].file}.all cannot be combined with columns of the same file"
            )
        return "file"
    return "multi_column" if len(columns) >= 2 else "column"


def validate_columns(refs: list[ColumnRef], catalog: Catalog) -> list[str]:
    errors: list[str] = []
    for ref in refs:
        if not catalog.has_file(ref.file):
            errors.append(f"file {ref.file} does not exist under database/")
        elif ref.column != WHOLE_FILE and not catalog.has_column(ref.file, ref.column):
            errors.append(f"column {ref.column} does not exist in {ref.file}")
    return errors


def check_body(rule: Mapping[str, str], catalog: Catalog) -> list[str]:
    """Report cell values in the body fields; concrete values belong in EXAMPLE_USAGE."""
    errors: list[str] = []
    for field in BODY_FIELDS:
        hits = catalog.find_cell_values(rule.get(field) or "")
        if hits:
            errors.append(
                f"{field.upper()} contains cell values {', '.join(hits)}; "
                "name only files and columns here and move concrete values to EXAMPLE_USAGE"
            )
    return errors


def check_scope_consistency(declared: str, refs: list[ColumnRef]) -> str | None:
    """Return an error message for the reflector/generator, or None if consistent."""
    scope = (declared or "").strip().lower()
    if scope not in SCOPES:
        return f"SCOPE {declared!r} is not one of {', '.join(SCOPES)}"
    derived = derive_scope(refs)
    if scope != derived:
        return (
            f"SCOPE says {scope} but COLUMNS imply {derived}; "
            "fix SCOPE or COLUMNS so they agree"
        )
    return None
