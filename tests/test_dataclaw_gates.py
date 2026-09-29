"""Deterministic gates a divergence must pass before it reaches rule generation."""

import pytest

from tkstore.dataclaw.catalog import Catalog
from tkstore.dataclaw.gates import ACCEPTED, LOGGED, REJECTED, check_divergence, missed_milestones
from tkstore.dataclaw.reflector_io import Divergence, Evidence, Reproduction
from tkstore.dataclaw.scope import ColumnRef

OPS = "enterprise/company_operation_status.csv"
PROFILE = "enterprise/company_profile.csv"

CATALOG = Catalog(
    headers={OPS: ["bmCode", "secondTargetNum", "targetName", "value"], PROFILE: ["bmCode", "province"]},
    values=frozenset(),
)
MILESTONES = {
    "Top province by revenue": "Beijing",
    "Number of companies": 1004,
    "Growth rate": 0.1234,
    "Already done": 13,
    "Provinces": ["Beijing", "Shanghai"],
    "Unknowable": None,
}
MISSED = {"Top province by revenue", "Number of companies", "Growth rate", "Provinces", "Unknowable"}
PROBES = {
    1: "secondTargetNum  targetName  rows\nY_EC_5   营收金额   1,004\nY_EC_5   营业收入金额  2778",
    2: "province,total\n北京市,1.2E+12\n上海市,9.1E+11",
    3: "growth=0.12345678",
}


def _divergence(**overrides):
    base = dict(
        index=1,
        divergence="filtered on one targetName spelling",
        needed="filter on secondTargetNum",
        scope="multi_column",
        columns=[ColumnRef(OPS, "secondTargetNum"), ColumnRef(OPS, "targetName")],
        fact="one secondTargetNum has several targetName spellings",
        category="同一指标有多个名字",
        evidence=[Evidence(1, "Y_EC_5 营收金额 1,004")],
        reproduced=[Reproduction("Number of companies", 1004, 1)],
        kind="data",
    )
    base.update(overrides)
    return Divergence(**base)


def _check(d):
    return check_divergence(d, probes=PROBES, catalog=CATALOG, milestones=MILESTONES, missed=MISSED)


def test_well_formed_divergence_is_accepted():
    result = _check(_divergence())
    assert result.status == ACCEPTED, result.reasons
    assert result.reasons == []


@pytest.mark.parametrize("kind", ["non_data", "gold_suspect"])
def test_non_data_and_gold_suspect_are_only_logged(kind):
    result = _check(_divergence(kind=kind, evidence=[], reproduced=[]))
    assert result.status == LOGGED


def test_parse_errors_reject():
    result = _check(_divergence(errors=["KIND 'maybe' is not one of data, non_data, gold_suspect"]))
    assert result.status == REJECTED
    assert "KIND" in result.reasons[0]


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"evidence": []}, "EVIDENCE"),
        ({"reproduced": []}, "REPRODUCED"),
        ({"fact": ""}, "FACT"),
        ({"scope": ""}, "SCOPE"),
    ],
)
def test_data_divergence_needs_every_field(overrides, fragment):
    result = _check(_divergence(**overrides))
    assert result.status == REJECTED
    assert any(fragment in r for r in result.reasons), result.reasons


def test_evidence_must_cite_an_existing_probe():
    result = _check(_divergence(evidence=[Evidence(9, "anything")]))
    assert any("probe#9" in r for r in result.reasons)


def test_evidence_excerpt_must_be_in_the_output():
    result = _check(_divergence(evidence=[Evidence(1, "Y_EC_5 营收金额 2000")]))
    assert result.status == REJECTED
    assert any("not found in the output of probe#1" in r for r in result.reasons)


def test_unknown_file_or_column_rejects():
    result = _check(_divergence(columns=[ColumnRef(OPS, "industry"), ColumnRef(OPS, "value")]))
    assert any("industry" in r for r in result.reasons)


def test_scope_must_match_tables_and_columns():
    result = _check(_divergence(scope="column"))
    assert any("multi_column" in r for r in result.reasons)


def test_unknown_milestone_key_lists_the_missed_ones():
    result = _check(_divergence(reproduced=[Reproduction("Number of firms", 1004, 1)]))
    reason = next(r for r in result.reasons if "Number of firms" in r)
    assert "Number of companies" in reason


def test_achieved_milestone_cannot_be_used():
    result = _check(_divergence(reproduced=[Reproduction("Already done", 13, 1)]))
    assert any("already achieved" in r for r in result.reasons)


def test_reproduced_value_must_appear_in_the_cited_probe():
    result = _check(_divergence(reproduced=[Reproduction("Number of companies", 1004, 3)]))
    assert any("does not appear in the output of probe#3" in r for r in result.reasons)


def test_numbers_are_found_regardless_of_formatting():
    result = _check(_divergence(reproduced=[Reproduction("Number of companies", 1004.0, 1)]))
    assert result.status == ACCEPTED, result.reasons


def test_value_off_by_more_than_one_percent_is_rejected():
    probes = {**PROBES, 4: "count 1020"}
    d = _divergence(reproduced=[Reproduction("Number of companies", 1020, 4)])
    result = check_divergence(d, probes=probes, catalog=CATALOG, milestones=MILESTONES, missed=MISSED)
    assert any("does not match gold" in r and "1004" in r for r in result.reasons)


def test_rounded_value_within_tolerance_is_accepted():
    d = _divergence(reproduced=[Reproduction("Growth rate", 0.12345678, 3)])
    assert _check(d).status == ACCEPTED


def test_translated_string_needs_semantic_match():
    without = _check(_divergence(reproduced=[Reproduction("Top province by revenue", "北京市", 2)]))
    assert without.status == REJECTED
    assert any("SEMANTIC_MATCH" in r for r in without.reasons)

    with_it = _check(
        _divergence(reproduced=[Reproduction("Top province by revenue", "北京市", 2, "北京市 is Beijing")])
    )
    assert with_it.status == ACCEPTED, with_it.reasons


def test_list_elements_must_all_appear():
    ok = _divergence(reproduced=[Reproduction("Provinces", ["北京市", "上海市"], 2, "Chinese names")])
    assert _check(ok).status == ACCEPTED
    missing = _divergence(reproduced=[Reproduction("Provinces", ["北京市", "广东省"], 2, "Chinese names")])
    assert any("广东省" in r for r in _check(missing).reasons)


def test_unverifiable_gold_is_rejected():
    result = _check(_divergence(reproduced=[Reproduction("Unknowable", "x", 1)]))
    assert any("cannot be verified" in r for r in result.reasons)


def test_one_bad_reproduction_rejects_the_whole_divergence():
    reproduced = [Reproduction("Number of companies", 1004, 1), Reproduction("Already done", 13, 1)]
    assert _check(_divergence(reproduced=reproduced)).status == REJECTED


def test_missed_milestones_from_process_score():
    process = {"gpr": {"details": [
        {"key": "A", "achieved": False},
        {"key": "B", "achieved": True},
        {"key": "C", "achieved": False},
    ]}}
    assert missed_milestones(process) == {"A", "C"}
    assert missed_milestones(None) == set()
    assert missed_milestones({"gpr": {}}) == set()
