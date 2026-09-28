"""Header catalog and cell-value set of a DataClaw database directory.

The value set backs the rule-body check: ENSURE / WHEN_TO_CHECK / CONTEXT may
name files and columns but not any concrete cell value. Only values of 3-40
characters are kept (pure numbers included); shorter ones collide with ordinary
prose and longer ones never appear verbatim in a rule body.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

MIN_VALUE_CHARS = 3
MAX_VALUE_CHARS = 40
DATA_SUFFIXES = (".csv", ".json")


@dataclass(frozen=True)
class Catalog:
    headers: dict[str, list[str]]
    values: frozenset[str]

    def has_file(self, file: str) -> bool:
        return file in self.headers

    def has_column(self, file: str, column: str) -> bool:
        return column in self.headers.get(file, ())

    def find_cell_values(self, text: str) -> list[str]:
        """Cell values occurring as substrings of ``text``, in order of first occurrence."""
        hits: dict[str, None] = {}
        for start in range(len(text)):
            longest = min(MAX_VALUE_CHARS, len(text) - start)
            for length in range(MIN_VALUE_CHARS, longest + 1):
                candidate = text[start : start + length]
                if candidate in self.values:
                    hits.setdefault(candidate)
        return list(hits)


def _data_files(data_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in data_dir.rglob("*")
        if path.is_file() and path.suffix in DATA_SUFFIXES
    )


def _json_strings(node: Any, top_level: bool = True) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if not top_level:
                yield str(key)
            yield from _json_strings(value, top_level=False)
    elif isinstance(node, list):
        for item in node:
            yield from _json_strings(item, top_level=False)
    elif isinstance(node, str):
        yield node


def _cells(path: Path, headers: dict[str, list[str]], rel: str) -> Iterator[str]:
    if path.suffix == ".json":
        headers[rel] = []
        yield from _json_strings(json.loads(path.read_text(encoding="utf-8")))
        return
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        headers[rel] = [name.strip() for name in next(reader, [])]
        for row in reader:
            yield from row


def build_catalog(data_dir: Path) -> Catalog:
    data_dir = Path(data_dir)
    csv.field_size_limit(1 << 30)
    headers: dict[str, list[str]] = {}
    values: set[str] = set()
    for path in _data_files(data_dir):
        rel = path.relative_to(data_dir).as_posix()
        for cell in _cells(path, headers, rel):
            cell = cell.strip()
            if MIN_VALUE_CHARS <= len(cell) <= MAX_VALUE_CHARS:
                values.add(cell)

    schema_names: set[str] = set()
    for rel, columns in headers.items():
        name = rel.rsplit("/", 1)[-1]
        schema_names.update((rel, name, name.rsplit(".", 1)[0], *columns))
    return Catalog(headers=headers, values=frozenset(values - schema_names))


def _cache_key(data_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in _data_files(data_dir):
        stat = path.stat()
        rel = path.relative_to(data_dir).as_posix()
        digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


def load_catalog(data_dir: Path, cache_dir: Path) -> Catalog:
    """Build the catalog once per data snapshot; the cache holds only the latest one."""
    data_dir, cache_dir = Path(data_dir), Path(cache_dir)
    cache_path = cache_dir / f"catalog_{_cache_key(data_dir)}.json"
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        return Catalog(headers=cached["headers"], values=frozenset(cached["values"]))

    catalog = build_catalog(data_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale in cache_dir.glob("catalog_*.json"):
        stale.unlink()
    tmp_path = cache_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(
            {"headers": catalog.headers, "values": sorted(catalog.values)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp_path.replace(cache_path)
    return catalog
