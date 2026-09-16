"""Bounding how long one refiner probe may run.

The refiner generates its own exploratory SQL, so it can emit a query the agent never
would. One such probe against `f1.sqlite` ran for four hours at a full core, reading
2.4 TB from the page cache, and stalled a whole 86-instance pass on instance 81.

The agent's own path has been bounded all along -- `SQLiteExecutor` interrupts after
120 seconds -- so this is a missing guard on the refiner's connection rather than a
missing mechanism.
"""

import sqlite3
import time

import pytest

from src.agents import cte_refiner


# `execute` steps until the first row is available, so where a runaway query hangs
# depends on the shape: an aggregate has to finish before it can yield a row, while a
# plain projection yields immediately and spins in the fetch instead.
RUNAWAY_IN_EXECUTE = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
)
RUNAWAY_IN_FETCH = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c"
)

BUDGET = 0.3


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    connection.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y")])
    yield connection
    connection.close()


class TestExecuteWithTimeout:
    def test_a_quick_query_returns_its_headers_and_rows(self, conn):
        headers, rows = cte_refiner._execute_with_timeout(
            conn, conn.cursor(), "SELECT a, b FROM t ORDER BY a"
        )

        assert headers == ["a", "b"]
        assert rows == [(1, "x"), (2, "y")]

    def test_a_query_that_never_yields_a_row_is_interrupted(self, conn):
        started = time.monotonic()

        with pytest.raises(TimeoutError, match="exceeded"):
            cte_refiner._execute_with_timeout(conn, conn.cursor(), RUNAWAY_IN_EXECUTE, seconds=BUDGET)

        assert time.monotonic() - started < 10

    def test_a_query_that_spins_in_the_fetch_is_interrupted_too(self, conn):
        """Bounding only `execute` would just move the hang into `fetchall`."""
        started = time.monotonic()

        with pytest.raises(TimeoutError, match="exceeded"):
            cte_refiner._execute_with_timeout(conn, conn.cursor(), RUNAWAY_IN_FETCH, seconds=BUDGET)

        assert time.monotonic() - started < 10

    def test_the_connection_still_works_after_an_interrupt(self, conn):
        """The refiner keeps one connection for all 25 turns, so an aborted probe has to
        leave it usable for the next one."""
        with pytest.raises(TimeoutError):
            cte_refiner._execute_with_timeout(conn, conn.cursor(), RUNAWAY_IN_EXECUTE, seconds=BUDGET)

        headers, rows = cte_refiner._execute_with_timeout(conn, conn.cursor(), "SELECT COUNT(*) FROM t")

        assert rows == [(2,)]

    def test_an_ordinary_sql_error_is_not_reported_as_a_timeout(self, conn):
        """The refiner feeds the message back to the model, so it has to say what is wrong."""
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            cte_refiner._execute_with_timeout(conn, conn.cursor(), "SELECT * FROM missing_table")

    def test_fetching_one_row_does_not_walk_an_unbounded_result(self, conn):
        """The compile checks only need to know the statement runs. Fetching everything
        would turn a cheap check into the very hang this guards against."""
        started = time.monotonic()

        headers, rows = cte_refiner._execute_with_timeout(
            conn, conn.cursor(), RUNAWAY_IN_FETCH, fetch_all=False, seconds=BUDGET
        )

        assert time.monotonic() - started < BUDGET
        assert len(rows) <= 1

    def test_the_default_budget_matches_the_agent_side(self):
        """Two different limits on the same database would be a trap for whoever next
        reads a timeout in the logs."""
        assert cte_refiner.QUERY_TIMEOUT_SECONDS == 120.0
