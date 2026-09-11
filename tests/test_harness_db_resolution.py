"""Database resolution inside the populate harness.

Regression guard for deviation C9: the inline resolution in
`run_diff_for_instance` joined DATA_BASE_FOLDER ('data') with the instance id,
producing `data/<instance_id>` instead of `data/spider2/<instance_id>`, so the
fallback never matched. Resolution must now go through the shared resolver.
"""

from pathlib import Path

from tkstore.harness import _resolve_db_path_or_cred
from tests.conftest import write_repo_map


def test_sqlite_instance_matches_shared_resolver(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {"local002": "E_commerce"})

    resolved = _resolve_db_path_or_cred(
        "local002",
        "E_commerce",
        "sqlite",
        db_root=fake_db_root,
        repo_root=fake_repo_root,
    )

    assert Path(resolved) == fake_db_root / "E_commerce.sqlite"


def test_sqlite_per_instance_directory_needs_spider2_segment(fake_repo_root, fake_db_root):
    """The old code looked in data/<id>; the database actually lives in data/spider2/<id>."""
    write_repo_map(fake_repo_root, {})
    instance_dir = fake_repo_root / "data" / "spider2" / "local500"
    instance_dir.mkdir(parents=True)
    expected = instance_dir / "Whatever.sqlite"
    expected.touch()

    resolved = _resolve_db_path_or_cred(
        "local500", "whatever", "sqlite", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert Path(resolved) == expected


def test_unresolvable_sqlite_returns_none(fake_repo_root, fake_db_root):
    write_repo_map(fake_repo_root, {})

    resolved = _resolve_db_path_or_cred(
        "local999", "nope", "sqlite", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert resolved is None


def test_snowflake_returns_credential_path(fake_repo_root, fake_db_root):
    resolved = _resolve_db_path_or_cred(
        "sf001", "SOME_DB", "snowflake", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert resolved == "src/executors/snowflake_credential.json"


def test_bigquery_returns_credential_path(fake_repo_root, fake_db_root):
    resolved = _resolve_db_path_or_cred(
        "bq001", "some-project", "bq", db_root=fake_db_root, repo_root=fake_repo_root
    )

    assert resolved == "src/executors/bigquery_credential.json"
