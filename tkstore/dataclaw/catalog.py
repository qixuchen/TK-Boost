"""Header catalog of a DataClaw database directory.

The gates use it to check that the files and columns a divergence names exist.
Only header lines are read; JSON files are listed without columns.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

DATA_SUFFIXES = (".csv", ".json")
CACHE_FORMAT = "headers-v2"


@dataclass(frozen=True)
class Catalog:
    headers: dict[str, list[str]]

    def has_file(self, file: str) -> bool:
        return file in self.headers

    def has_column(self, file: str, column: str) -> bool:
        return column in self.headers.get(file, ())


def _data_files(data_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in data_dir.rglob("*")
        if path.is_file() and path.suffix in DATA_SUFFIXES
    )


def _header(path: Path) -> list[str]:
    if path.suffix == ".json":
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [name.strip() for name in next(csv.reader(fh), [])]


def build_catalog(data_dir: Path) -> Catalog:
    data_dir = Path(data_dir)
    csv.field_size_limit(1 << 30)
    return Catalog(headers={
        path.relative_to(data_dir).as_posix(): _header(path) for path in _data_files(data_dir)
    })


def _cache_key(data_dir: Path) -> str:
    digest = hashlib.sha256(f"{CACHE_FORMAT}\n".encode())
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
        return Catalog(headers=json.loads(cache_path.read_text(encoding="utf-8"))["headers"])

    catalog = build_catalog(data_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale in cache_dir.glob("catalog_*.json"):
        stale.unlink()
    tmp_path = cache_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({"headers": catalog.headers}, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(cache_path)
    return catalog
