"""TABLES / COLUMNS parsing, SCOPE derivation and existence checks for divergences.

A TABLES entry is a file path relative to DataClaw's ``database/`` directory
ending in ``.csv`` or ``.json``, used when a fact concerns the whole file. A
COLUMNS entry is ``<file>.<column>``. Both empty means a generic rule.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from .catalog import Catalog

_FILE = re.compile(r"^.+\.(?:csv|json)$")
_COLUMN = re.compile(r"^(?P<file>.+?\.(?:csv|json))\.(?P<column>.+)$")
_DATABASE_PREFIX = re.compile(r"^(?:\./)?database/")


class ColumnRef(NamedTuple):
    file: str
    column: str


def _entries(text: str) -> list[tuple[str, str]]:
    """(raw, normalized) pairs of the comma/newline separated entries."""
    pairs = []
    for raw in re.split(r"[,\n]", text or ""):
        entry = _DATABASE_PREFIX.sub("", raw.strip())
        if entry:
            pairs.append((raw.strip(), entry))
    return pairs


def parse_tables(text: str) -> list[str]:
    tables: list[str] = []
    for raw, entry in _entries(text):
        if not _FILE.match(entry):
            raise ValueError(f"TABLES entry {raw!r} is not a .csv or .json file under database/")
        if entry not in tables:
            tables.append(entry)
    return tables


def parse_columns(text: str) -> list[ColumnRef]:
    refs: list[ColumnRef] = []
    for raw, entry in _entries(text):
        match = _COLUMN.match(entry)
        column = match.group("column").strip() if match else ""
        if not column or column == "all":
            raise ValueError(
                f"COLUMNS entry {raw!r} is not <file>.<column>; list whole files under TABLES"
            )
        ref = ColumnRef(match.group("file"), column)
        if ref not in refs:
            refs.append(ref)
    return refs


def derive_scope(tables: list[str], columns: list[ColumnRef]) -> str:
    files = set(tables) | {ref.file for ref in columns}
    if not files:
        return "generic"
    if len(files) >= 2:
        return "cross_table"
    if tables:
        return "file"
    return "multi_column" if len({ref.column for ref in columns}) >= 2 else "column"


def validate_refs(tables: list[str], columns: list[ColumnRef], catalog: Catalog) -> list[str]:
    errors: list[str] = []
    for table in tables:
        if not catalog.has_file(table):
            errors.append(f"TABLES file {table} does not exist under database/")
    for ref in columns:
        if not catalog.has_file(ref.file):
            errors.append(f"COLUMNS file {ref.file} does not exist under database/")
        elif not catalog.has_column(ref.file, ref.column):
            errors.append(f"column {ref.column} does not exist in {ref.file}")
    return errors
