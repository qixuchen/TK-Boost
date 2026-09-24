"""SQLiteExecutor timeout is configurable so long gold queries can finish."""

import sqlite3
from pathlib import Path

import pytest

from src.executors.sqlite_executor import SQLiteExecutor


def _empty_db(tmp_path: Path) -> str:
    path = tmp_path / "t.sqlite"
    sqlite3.connect(path).close()
    return str(path)


def test_execute_interrupts_when_timeout_seconds_elapses(tmp_path):
    db = _empty_db(tmp_path)
    slow = (
        "WITH RECURSIVE r(n) AS ("
        " SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 1000000000"
        ") SELECT COUNT(*) FROM r"
    )
    executor = SQLiteExecutor(db, timeout_seconds=0.5)

    with pytest.raises(TimeoutError, match="0.5"):
        executor.execute(slow)


def test_default_timeout_remains_120_seconds():
    assert SQLiteExecutor("unused.sqlite").timeout_seconds == 120.0
