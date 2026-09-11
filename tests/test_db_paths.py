"""Behaviour of SQLite database path resolution.

Covers the resolution order documented in docs/implementation_plan.md section 1.1:

    1. minidev branch
    2. instance -> basename map (vendored copy first, shared directory second)
    3. map miss but db_id given -> <db_root>/<db_id>.sqlite
    4. <repo_root>/data/spider2/<instance_id>/*.sqlite
    5. <repo_root>/<db_id>.sqlite
"""

import os
from pathlib import Path

import pytest

from src.utils.db_paths import (
    DbPathNotFound,
    get_database_path,
    resolve_sqlite_db_path,
)
from tests.conftest import write_repo_map


def test_vendored_map_hit_returns_db_root_path(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {"local002": "E_commerce"})

    resolved = get_database_path(
        "local002", "E_commerce", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == fake_db_root / "E_commerce.sqlite"


@pytest.mark.parametrize(
    "instance_id,basename",
    [("local096", "Db-IMDB"), ("local056", "sqlite-sakila")],
)
def test_irregular_basenames_resolve(
    fake_repo_root, fake_db_root, instance_id, basename
):
    """Regression guard for the old lowercase/strip-separator heuristic."""
    write_repo_map(fake_repo_root, {instance_id: basename})

    resolved = get_database_path(
        instance_id, basename, db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == fake_db_root / f"{basename}.sqlite"


def test_vendored_map_wins_over_shared_map(fake_repo_root, fake_db_root):
    """The shared map says Baseball; the vendored map must take precedence."""
    write_repo_map(fake_repo_root, {"local007": "E_commerce"})

    resolved = get_database_path(
        "local007", None, db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == fake_db_root / "E_commerce.sqlite"


def test_falls_back_to_shared_map_when_vendored_absent(fake_repo_root, fake_db_root):
    resolved = get_database_path(
        "local007", None, db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == fake_db_root / "Baseball.sqlite"


def test_map_miss_uses_db_id_as_basename(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {})

    resolved = get_database_path(
        "local999", "Baseball", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == fake_db_root / "Baseball.sqlite"


def test_map_wins_when_it_disagrees_with_db_id(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {"local002": "E_commerce"})

    resolved = get_database_path(
        "local002", "Baseball", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == fake_db_root / "E_commerce.sqlite"


def test_falls_back_to_per_instance_directory(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {})
    instance_dir = fake_repo_root / "data" / "spider2" / "local500"
    instance_dir.mkdir(parents=True)
    (instance_dir / "DDL.csv").touch()
    expected = instance_dir / "Whatever.sqlite"
    expected.touch()

    resolved = get_database_path(
        "local500", "unknown-db", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == expected


def test_minidev_branch(fake_repo_root, fake_db_root):
    db_dir = fake_repo_root / "data" / "minidev" / "MINIDEV" / "dev_databases" / "financial"
    db_dir.mkdir(parents=True)
    expected = db_dir / "financial.sqlite"
    expected.touch()

    resolved = get_database_path(
        "minidev042", "financial", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == expected


def test_returns_absolute_path(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {"local002": "E_commerce"})

    resolved = get_database_path(
        "local002", None, db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert os.path.isabs(resolved)


def test_resolution_is_cwd_independent(fake_repo_root, fake_db_root, monkeypatch, tmp_path):
    write_repo_map(fake_repo_root, {"local002": "E_commerce"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = get_database_path(
        "local002", None, db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == fake_db_root / "E_commerce.sqlite"


def test_failure_raises_with_ordered_attempted_paths(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {})

    with pytest.raises(DbPathNotFound) as excinfo:
        get_database_path(
            "local999", "no-such-db", db_root=fake_db_root, repo_root=fake_repo_root
        )

    error = excinfo.value
    assert error.instance_id == "local999"
    assert error.db_id == "no-such-db"
    assert error.attempted, "attempted paths must be reported for diagnosis"
    assert all(os.path.isabs(p) for p in error.attempted)
    # db_root candidates are tried before the per-instance directory fallback.
    db_root_idx = next(
        i for i, p in enumerate(error.attempted) if str(fake_db_root) in p
    )
    instance_dir_idx = next(
        i for i, p in enumerate(error.attempted) if "data/spider2/local999" in p
    )
    assert db_root_idx < instance_dir_idx


def test_missing_db_root_still_tries_repo_paths_and_hints(fake_repo_root):
    """SPIDER2_DB_ROOT unset must not short-circuit the repo-local branches."""
    instance_dir = fake_repo_root / "data" / "spider2" / "local500"
    instance_dir.mkdir(parents=True)
    expected = instance_dir / "Whatever.sqlite"
    expected.touch()

    resolved = get_database_path("local500", None, db_root=None, repo_root=fake_repo_root)
    assert Path(resolved) == expected

    with pytest.raises(DbPathNotFound) as excinfo:
        get_database_path("local999", "nope", db_root=None, repo_root=fake_repo_root)
    assert any("SPIDER2_DB_ROOT" in hint for hint in excinfo.value.hints)


def test_shim_returns_none_instead_of_raising(fake_repo_root, monkeypatch):
    monkeypatch.setattr("src.utils.db_paths._default_repo_root", lambda: fake_repo_root)

    assert resolve_sqlite_db_path("local999", "no-such-db") is None


def test_db_root_read_from_environment(fake_repo_root, fake_db_root, monkeypatch):
    write_repo_map(fake_repo_root, {"local002": "E_commerce"})
    monkeypatch.setenv("SPIDER2_DB_ROOT", str(fake_db_root))
    monkeypatch.setattr("src.utils.db_paths._default_repo_root", lambda: fake_repo_root)

    assert Path(resolve_sqlite_db_path("local002")) == fake_db_root / "E_commerce.sqlite"


class TestDbRootDiscovery:
    """`SPIDER2_DB_ROOT` is mandatory once the per-instance symlinks are gone, so
    it is also discovered from the repository's .env file."""

    def test_falls_back_to_dotenv_when_env_var_absent(self, fake_repo_root, fake_db_root):
        write_repo_map(fake_repo_root, {"local002": "E_commerce"})
        (fake_repo_root / ".env").write_text(
            f'SPIDER2_DB_ROOT="{fake_db_root}"\n', encoding="utf-8"
        )

        resolved = get_database_path("local002", None, repo_root=fake_repo_root)

        assert Path(resolved) == fake_db_root / "E_commerce.sqlite"

    def test_dotenv_accepts_export_prefix(self, fake_repo_root, fake_db_root):
        write_repo_map(fake_repo_root, {"local002": "E_commerce"})
        (fake_repo_root / ".env").write_text(
            f"export SPIDER2_DB_ROOT={fake_db_root}\n", encoding="utf-8"
        )

        resolved = get_database_path("local002", None, repo_root=fake_repo_root)

        assert Path(resolved) == fake_db_root / "E_commerce.sqlite"

    def test_environment_wins_over_dotenv(self, fake_repo_root, fake_db_root, monkeypatch):
        write_repo_map(fake_repo_root, {"local002": "E_commerce"})
        (fake_repo_root / ".env").write_text(
            "SPIDER2_DB_ROOT=/nonexistent/from/dotenv\n", encoding="utf-8"
        )
        monkeypatch.setenv("SPIDER2_DB_ROOT", str(fake_db_root))

        resolved = get_database_path("local002", None, repo_root=fake_repo_root)

        assert Path(resolved) == fake_db_root / "E_commerce.sqlite"

    def test_explicit_argument_wins_over_dotenv(self, fake_repo_root, fake_db_root):
        write_repo_map(fake_repo_root, {"local002": "E_commerce"})
        (fake_repo_root / ".env").write_text(
            "SPIDER2_DB_ROOT=/nonexistent/from/dotenv\n", encoding="utf-8"
        )

        resolved = get_database_path(
            "local002", None, db_root=fake_db_root, repo_root=fake_repo_root
        )

        assert Path(resolved) == fake_db_root / "E_commerce.sqlite"

    def test_dotenv_without_the_key_is_ignored(self, fake_repo_root):
        write_repo_map(fake_repo_root, {"local002": "E_commerce"})
        (fake_repo_root / ".env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")

        with pytest.raises(DbPathNotFound) as excinfo:
            get_database_path("local002", None, repo_root=fake_repo_root)

        assert any("SPIDER2_DB_ROOT" in hint for hint in excinfo.value.hints)

    def test_dotenv_discovery_does_not_leak_into_environment(self, fake_repo_root, fake_db_root):
        """Reading .env here must not mutate os.environ for unrelated keys."""
        write_repo_map(fake_repo_root, {"local002": "E_commerce"})
        (fake_repo_root / ".env").write_text(
            f"SPIDER2_DB_ROOT={fake_db_root}\nSOME_OTHER_KEY=leaked\n", encoding="utf-8"
        )

        get_database_path("local002", None, repo_root=fake_repo_root)

        assert "SOME_OTHER_KEY" not in os.environ
