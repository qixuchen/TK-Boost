"""CTE splitting drives the whole per-CTE retrieval loop of Algorithm 5.

When this parser returns no CTEs the loop is skipped entirely and no per-CTE
knowledge is ever retrieved, so a mis-parse is silent and costs a whole instance.
"""

from src.utils.agent_utils import parse_ctes_from_sql


def _names(sql: str) -> list:
    ctes, _ = parse_ctes_from_sql(sql)
    return [c["name"] for c in ctes]


class TestLeadingComments:
    """`WITH` must be located as SQL, not as the English word in a comment."""

    def test_ignores_the_word_with_in_a_leading_line_comment(self):
        sql = """-- delivered orders with pizza_id and exclusions
WITH delivered_orders AS (
SELECT order_id FROM orders
)
SELECT * FROM delivered_orders"""

        ctes, remainder = parse_ctes_from_sql(sql)

        assert [c["name"] for c in ctes] == ["delivered_orders"]
        assert remainder == "SELECT * FROM delivered_orders"

    def test_ignores_the_word_with_in_a_leading_block_comment(self):
        sql = """/* joins customers with orders */
WITH delivered_orders AS (
SELECT order_id FROM orders
)
SELECT * FROM delivered_orders"""

        assert _names(sql) == ["delivered_orders"]

    def test_ignores_the_word_with_across_several_comment_lines(self):
        """The shape that made local066 parse to zero CTEs."""
        sql = """-- Main working CTEs:
-- 1. delivered_orders: IDs of delivered orders
-- 2. order_details: delivered orders with pizza_id, base toppings, exclusions
WITH delivered_orders AS (
SELECT order_id FROM orders WHERE status = 'delivered'
),
order_details AS (
SELECT order_id, pizza_id FROM delivered_orders
)
SELECT * FROM order_details"""

        assert _names(sql) == ["delivered_orders", "order_details"]

    def test_returns_no_ctes_when_only_comments_mention_with(self):
        sql = """-- pairs customers with orders
SELECT * FROM orders"""

        ctes, remainder = parse_ctes_from_sql(sql)

        assert ctes == []
        assert remainder == sql


class TestRemainderBoundary:
    """Where the CTE list ends must not depend on how long the next name is."""

    def test_parses_two_ctes_with_short_names(self):
        sql = """WITH a AS (
SELECT 1 AS x FROM t1
),
b AS (
SELECT 2 AS y FROM t2
)
SELECT * FROM b"""

        ctes, remainder = parse_ctes_from_sql(sql)

        assert [c["name"] for c in ctes] == ["a", "b"]
        assert remainder == "SELECT * FROM b"

    def test_short_and_long_names_parse_alike(self):
        short = """WITH a AS (
SELECT 1
),
b AS (
SELECT 2
)
SELECT * FROM b"""
        long = short.replace("a AS", "customer_totals AS").replace("b AS", "monthly_revenue AS")

        assert len(_names(short)) == len(_names(long)) == 2

    def test_remainder_starts_at_a_select_on_the_same_line(self):
        sql = "WITH a AS (SELECT 1) SELECT * FROM a"

        ctes, remainder = parse_ctes_from_sql(sql)

        assert [c["name"] for c in ctes] == ["a"]
        assert remainder == "SELECT * FROM a"

    def test_comment_between_two_ctes(self):
        sql = """WITH a AS (
SELECT 1
),
-- now aggregate
b AS (
SELECT 2
)
SELECT * FROM b"""

        assert _names(sql) == ["a", "b"]

    def test_remainder_is_empty_when_nothing_follows_the_last_cte(self):
        sql = """WITH a AS (
SELECT 1
)"""

        ctes, remainder = parse_ctes_from_sql(sql)

        assert [c["name"] for c in ctes] == ["a"]
        assert remainder == ""


class TestCteHeaderSyntax:
    """Everything between `WITH` and the body's opening paren."""

    def test_with_recursive(self):
        sql = """WITH RECURSIVE packaging_expansion AS (
SELECT packaging_id FROM packaging
)
SELECT * FROM packaging_expansion"""

        ctes, remainder = parse_ctes_from_sql(sql)

        assert [c["name"] for c in ctes] == ["packaging_expansion"]
        assert remainder == "SELECT * FROM packaging_expansion"

    def test_with_recursive_and_several_ctes(self):
        sql = """WITH RECURSIVE expansion AS (
SELECT 1 AS id
),
rolled_up AS (
SELECT id FROM expansion
)
SELECT * FROM rolled_up"""

        assert _names(sql) == ["expansion", "rolled_up"]

    def test_cte_with_a_column_list(self):
        sql = """WITH months(month) AS (
SELECT '2020-01'
)
SELECT * FROM months"""

        ctes, _ = parse_ctes_from_sql(sql)

        assert [c["name"] for c in ctes] == ["months"]
        assert ctes[0]["body"] == "SELECT '2020-01'"

    def test_cte_with_several_named_columns(self):
        sql = """WITH balances(customer_id, month, closing) AS (
SELECT 1, '2020-01', 0
)
SELECT * FROM balances"""

        assert _names(sql) == ["balances"]

    def test_column_list_after_a_newline_following_with(self):
        """local074's exact shape."""
        sql = """WITH
months(month) AS (
  -- Generate months
  SELECT '2020-01' AS month
)
SELECT * FROM months"""

        assert _names(sql) == ["months"]

    def test_recursive_together_with_a_column_list(self):
        sql = """WITH RECURSIVE tree(id, parent) AS (
SELECT 1, NULL
)
SELECT * FROM tree"""

        assert _names(sql) == ["tree"]


class TestExistingBehaviour:
    """Characterisation guards for shapes seen in real agent output."""

    def test_plain_single_cte(self):
        sql = """WITH customer_rfm AS (
SELECT customer_id FROM customers
)
SELECT * FROM customer_rfm"""

        ctes, remainder = parse_ctes_from_sql(sql)

        assert [c["name"] for c in ctes] == ["customer_rfm"]
        assert ctes[0]["body"] == "SELECT customer_id FROM customers"
        assert remainder == "SELECT * FROM customer_rfm"

    def test_comment_inside_a_cte_body_is_kept(self):
        sql = """WITH order_total_payments AS (
    -- Aggregate payments per order
SELECT order_id FROM payments
)
SELECT * FROM order_total_payments"""

        ctes, _ = parse_ctes_from_sql(sql)

        assert "-- Aggregate payments per order" in ctes[0]["body"]

    def test_nested_parentheses_in_a_body(self):
        sql = """WITH ranked AS (
SELECT id, (SELECT max(v) FROM other) AS m FROM t
)
SELECT * FROM ranked"""

        ctes, _ = parse_ctes_from_sql(sql)

        assert len(ctes) == 1
        assert ctes[0]["body"].endswith("FROM t")

    def test_with_nested_inside_a_subquery(self):
        """local017's shape: the outer statement starts with SELECT."""
        sql = """SELECT year
FROM (
  WITH top_causes_per_year AS (
SELECT year FROM collisions
)
SELECT year FROM top_causes_per_year
) sub"""

        assert _names(sql) == ["top_causes_per_year"]

    def test_no_with_at_all(self):
        sql = "SELECT * FROM orders"

        ctes, remainder = parse_ctes_from_sql(sql)

        assert ctes == []
        assert remainder == sql
