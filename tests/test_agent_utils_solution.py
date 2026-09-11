"""Extraction of the agent's final SQL from a model response.

Observed on the Spider2 train run: several instances wrote the literal string
``<solution>`` into their reasoning prose before emitting the real block, which
poisoned the saved ``execution_query.sql``.
"""

from src.utils.agent_utils import detect_solution


def test_returns_none_when_no_solution_block():
    assert detect_solution("<sql>SELECT 1;</sql>") is None


def test_extracts_the_only_solution_block():
    content = "<solution>\nSELECT 1;\n</solution>"
    assert detect_solution(content) == "SELECT 1;"


def test_ignores_a_solution_tag_mentioned_in_reasoning_prose():
    content = (
        "<think>\n"
        "Checks pass. Ready to submit the final <solution>.\n"
        "</think><solution>\n"
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t;\n"
        "</solution>"
    )

    assert detect_solution(content) == "WITH t AS (SELECT 1 AS a) SELECT a FROM t;"


def test_takes_the_last_opening_tag_when_several_are_mentioned():
    content = (
        "First I will describe <solution> then mention <solution> again.\n"
        "<solution>SELECT 2;</solution>"
    )

    assert detect_solution(content) == "SELECT 2;"


def test_does_not_span_across_two_complete_solution_blocks():
    content = "<solution>SELECT 1;</solution>\nlater\n<solution>SELECT 2;</solution>"

    assert detect_solution(content) == "SELECT 1;"
