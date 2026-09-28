"""Milestone comparison used by the reflector's reproduction gate."""

import math

import pytest

from tkstore.dataclaw.milestones import (
    MATCH,
    MISMATCH,
    SEMANTIC,
    UNVERIFIABLE,
    compare,
    find_milestone,
)


@pytest.mark.parametrize(
    "expected, reproduced",
    [
        (0.67, 0.67),
        (0.67, 0.6766),  # 0.99% off
        (13, 13.0),
        (100, 99.0),  # exactly 1%
        (0.0, 5e-7),  # near zero uses the absolute tolerance
        (1234567, "1,234,567"),
        (0.67, " 0.67 "),
        (12.5, "12.5%"),
        ("185", 185),
    ],
)
def test_numeric_match(expected, reproduced):
    assert compare(expected, reproduced) == MATCH


@pytest.mark.parametrize(
    "expected, reproduced",
    [
        (0.67, 0.68),  # 1.5% off
        (100, 98.9),
        (0.0, 1e-5),
        (13, "thirteen"),
        (13, True),
        (0.67, math.nan),
    ],
)
def test_numeric_mismatch(expected, reproduced):
    assert compare(expected, reproduced) == MISMATCH


def test_string_equal_after_whitespace_and_case_normalization():
    assert compare("Guangdong Province", "  guangdong   province ") == MATCH


def test_translated_string_is_left_to_semantic_judgment():
    assert compare("Guangdong Province", "广东省") == SEMANTIC


def test_string_expected_with_numeric_reproduction_is_mismatch():
    assert compare("Shanghai", 3.2) == MISMATCH


@pytest.mark.parametrize(
    "expected, reproduced, verdict",
    [
        (True, True, MATCH),
        (True, "true", MATCH),
        (False, "False", MATCH),
        (True, False, MISMATCH),
        (True, 1, MISMATCH),
    ],
)
def test_bool(expected, reproduced, verdict):
    assert compare(expected, reproduced) == verdict


def test_none_expected_is_unverifiable():
    assert compare(None, "anything") == UNVERIFIABLE


def test_list_compared_as_set():
    assert compare(["Beijing", "Shanghai"], ["shanghai", "Beijing", "Beijing"]) == MATCH


def test_numeric_list_uses_tolerance_one_to_one():
    assert compare([1.0, 2.0], [2.005, 0.995]) == MATCH
    assert compare([1.0, 2.0], [1.0, 1.001]) == MISMATCH


def test_list_with_untranslated_strings_is_semantic():
    assert compare(["Beijing", "Shanghai"], ["北京市", "Shanghai"]) == SEMANTIC


def test_list_of_different_size_is_mismatch():
    assert compare(["Beijing", "Shanghai"], ["北京市"]) == MISMATCH


def test_list_expected_with_scalar_reproduction_is_mismatch():
    assert compare(["Beijing"], "Beijing") == MISMATCH


def test_list_of_dicts_is_unverifiable():
    assert compare([{"a": 1}], [{"a": 1}]) == UNVERIFIABLE


def test_dict_compared_by_gold_keys():
    expected = {"Real Estate": 416, "Mining": 12}
    assert compare(expected, {"real estate": 416.0, "Mining": 12}) == MATCH
    assert compare(expected, {"Real Estate": 416, "Mining": 20}) == MISMATCH


def test_dict_with_missing_or_translated_key_is_mismatch():
    assert compare({"Real Estate": 416}, {"房地产业": 416}) == MISMATCH


def test_dict_with_translated_string_value_is_semantic():
    assert compare({"top": "Guangdong Province", "n": 13}, {"top": "广东省", "n": 13}) == SEMANTIC


def test_dict_mismatch_outranks_semantic():
    assert compare({"top": "Guangdong Province", "n": 13}, {"top": "广东省", "n": 14}) == MISMATCH


def test_find_milestone_normalizes_whitespace_only():
    milestones = {"Number of valid provinces": 13, "Top province": "Zhejiang"}
    assert find_milestone(milestones, "  Number of  valid provinces ") == (
        "Number of valid provinces",
        13,
    )
    assert find_milestone(milestones, "number of valid provinces") is None
    assert find_milestone(milestones, "Top") is None
