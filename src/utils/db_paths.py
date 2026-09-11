"""Resolve the on-disk SQLite database for a benchmark instance.

Spider2 ships an official instance -> database-basename map, so resolution is an
exact lookup; no name normalisation is involved. A vendored copy of that map
lives at `data/spider2_local_map.json` and takes precedence over the one in the
shared database directory.

`SPIDER2_DB_ROOT` points at the shared directory holding the `.sqlite` files. It
has no default: when unset, the branches that need it are skipped and the
remaining repository-local branches are still attempted. It is read from the
environment first and from the repository's `.env` second, so entry points that
never call `tkboost.init()` still find it.
"""

import json
import os
from pathlib import Path
from typing import List, Optional

_VENDORED_MAP_RELPATH = ("data", "spider2_local_map.json")
_SHARED_MAP_FILENAME = "local-map.jsonl"
_DB_ROOT_ENV = "SPIDER2_DB_ROOT"


class DbPathNotFound(Exception):
    """Raised when no candidate path exists, carrying the paths that were tried."""

    def __init__(
        self,
        instance_id: str,
        db_id: Optional[str],
        attempted: List[str],
        hints: Optional[List[str]] = None,
    ):
        self.instance_id = instance_id
        self.db_id = db_id
        self.attempted = attempted
        self.hints = hints or []
        message = f"Could not resolve SQLite DB for instance {instance_id!r} (db_id={db_id!r})."
        if attempted:
            message += "\nTried:\n" + "\n".join(f"  - {p}" for p in attempted)
        if self.hints:
            message += "\nHints:\n" + "\n".join(f"  - {h}" for h in self.hints)
        super().__init__(message)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _read_dotenv_value(dotenv_path: Path, key: str) -> Optional[str]:
    """Read one ``KEY=value`` / ``export KEY=value`` entry without touching os.environ."""
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value or None
    return None


def _discover_db_root(repo_root: Path) -> Optional[Path]:
    """Environment first, then the repository's `.env`."""
    raw = os.environ.get(_DB_ROOT_ENV) or _read_dotenv_value(repo_root / ".env", _DB_ROOT_ENV)
    return Path(raw).expanduser() if raw else None


def _load_map(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lookup_basename(instance_id: str, repo_root: Path, db_root: Optional[Path]) -> Optional[str]:
    """Vendored map first, shared-directory map second."""
    candidates = [repo_root.joinpath(*_VENDORED_MAP_RELPATH)]
    if db_root is not None:
        candidates.append(db_root / _SHARED_MAP_FILENAME)
    for map_path in candidates:
        basename = _load_map(map_path).get(instance_id)
        if basename:
            return str(basename)
    return None


def get_database_path(
    instance_id: str,
    db_id: Optional[str] = None,
    *,
    db_root: Optional[os.PathLike] = None,
    repo_root: Optional[os.PathLike] = None,
) -> str:
    """Return the absolute path to the instance's SQLite database.

    Raises:
        DbPathNotFound: no candidate path exists. The exception lists every path
            that was tried, in order.
    """
    repo = Path(repo_root).expanduser() if repo_root is not None else _default_repo_root()
    root = Path(db_root).expanduser() if db_root is not None else _discover_db_root(repo)

    attempted: List[str] = []
    hints: List[str] = []
    if root is None:
        hints.append(
            f"{_DB_ROOT_ENV} is set neither in the environment nor in {repo / '.env'}, "
            "so the shared database directory was skipped."
        )

    def accept(candidate: Path) -> Optional[str]:
        resolved = candidate.resolve()
        attempted.append(str(resolved))
        return str(resolved) if candidate.exists() else None

    # 1. BIRD / mini-dev layout.
    if instance_id.lower().startswith("minidev") and db_id:
        hit = accept(repo / "data" / "minidev" / "MINIDEV" / "dev_databases" / db_id / f"{db_id}.sqlite")
        if hit:
            return hit

    # 2/3. Shared directory, keyed by the official map or by db_id as a fallback.
    if root is not None:
        basename = _lookup_basename(instance_id, repo, root)
        if basename is None and db_id:
            basename = db_id
        if basename:
            hit = accept(root / f"{basename}.sqlite")
            if hit:
                return hit

    # 4. Legacy per-instance directory.
    instance_dir = repo / "data" / "spider2" / instance_id
    attempted.append(str(instance_dir.resolve()))
    if instance_dir.is_dir():
        for entry in sorted(os.listdir(instance_dir)):
            if entry.endswith(".sqlite"):
                return str((instance_dir / entry).resolve())

    # 5. Legacy bare filename at the repository root.
    if db_id:
        hit = accept(repo / f"{db_id}.sqlite")
        if hit:
            return hit

    raise DbPathNotFound(instance_id, db_id, attempted, hints)


def resolve_sqlite_db_path(instance_id: str, db_id: Optional[str] = None) -> Optional[str]:
    """`get_database_path` for callers that treat failure as ``None``."""
    try:
        return get_database_path(instance_id, db_id, repo_root=_default_repo_root())
    except DbPathNotFound as exc:
        print(f"⚠️  {exc}")
        return None
