"""Shared fixtures.

Tests must stay deterministic and independent of the developer's local data, so
`SPIDER2_DB_ROOT` is unset for every test and the fake roots below are injected
explicitly rather than discovered from the filesystem.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Allow `import src...` when pytest is invoked from the repository root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def unset_db_root_env(monkeypatch):
    """Keep the real SPIDER2_DB_ROOT out of every test."""
    monkeypatch.delenv("SPIDER2_DB_ROOT", raising=False)


@pytest.fixture
def fake_repo_root(tmp_path):
    """A stand-in for the repository root, with an empty `data/` tree."""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def fake_db_root(tmp_path):
    """A stand-in for the shared SQLite directory.

    Includes the irregular basenames that broke the old normalised-name
    heuristic (`Db-IMDB`, `sqlite-sakila`).
    """
    root = tmp_path / "dbroot"
    root.mkdir()
    for name in ("E_commerce", "Db-IMDB", "sqlite-sakila", "Baseball"):
        (root / f"{name}.sqlite").touch()
    mapping = {
        "local002": "E_commerce",
        "local096": "Db-IMDB",
        "local056": "sqlite-sakila",
        "local007": "Baseball",
    }
    (root / "local-map.jsonl").write_text(json.dumps(mapping), encoding="utf-8")
    return root


def write_repo_map(repo_root: Path, mapping: dict) -> Path:
    """Write the vendored instance -> database-basename map."""
    path = repo_root / "data" / "spider2_local_map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path
