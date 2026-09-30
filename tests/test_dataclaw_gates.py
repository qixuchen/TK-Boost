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
    values=frozenset({"营收金额"}),
)
TASK_PROMPT = "Which province had the highest total   operating revenue in Shanghai's peer group?"
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
        basis="data",
        instance="the agent kept only the rows of one spelling for the companies in the task",
        generality=[Evidence(1, "Y_EC_5 营业收入金额 2778")],
        ensure="Select an indicator by secondTargetNum and keep every targetName under it.",
        when_to_check="The question uses a company-level indicator.",
        trigger="highest total operating revenue",
        context="Each company uses one targetName spelling for a given secondTargetNum.",
        example_usage="Y_EC_5 is spelled 营收金额 for some companies.",
    )
    base.update(overrides)
    return Divergence(**base)


def _check(d, probes=PROBES):
    return check_divergence(
        d, probes=probes, catalog=CATALOG, milestones=MILESTONES, missed=MISSED, task_prompt=TASK_PROMPT
    )


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
        ({"needed": ""}, "NEEDED"),
        ({"instance": ""}, "INSTANCE"),
        ({"basis": ""}, "BASIS"),
        ({"generality": []}, "GENERALITY"),
        ({"ensure": ""}, "ENSURE"),
        ({"when_to_check": ""}, "WHEN_TO_CHECK"),
        ({"context": ""}, "CONTEXT"),
        ({"example_usage": ""}, "EXAMPLE_USAGE"),
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
    result = _check(d, probes=probes)
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


def test_unknown_basis_is_rejected():
    result = _check(_divergence(basis="reference"))
    assert any("BASIS 'reference'" in r and "gold_only" in r for r in result.reasons), result.reasons


@pytest.mark.parametrize("basis", ["data", "gold_only"])
def test_basis_other_than_task_needs_no_quote(basis):
    assert _check(_divergence(basis=basis, basis_quote="not in the task")).status == ACCEPTED


def test_task_basis_needs_a_quote():
    result = _check(_divergence(basis="task"))
    assert any("BASIS_QUOTE" in r and "missing" in r for r in result.reasons), result.reasons


def test_task_quote_matches_the_task_ignoring_case_and_whitespace():
    ok = _divergence(basis="task", basis_quote="Highest Total operating\n revenue in shanghai's")
    assert _check(ok).status == ACCEPTED, _check(ok).reasons


def test_task_quote_not_in_the_task_is_rejected():
    result = _check(_divergence(basis="task", basis_quote="in Beijing"))
    assert any("BASIS_QUOTE 'in Beijing'" in r and "task" in r for r in result.reasons), result.reasons


def test_generality_must_cite_an_existing_probe():
    result = _check(_divergence(generality=[Evidence(9, "anything")]))
    assert any("GENERALITY cites probe#9" in r for r in result.reasons), result.reasons


def test_generality_excerpt_must_be_in_the_output():
    result = _check(_divergence(generality=[Evidence(1, "Y_EC_5 营业收入金额 9999")]))
    assert any("GENERALITY excerpt" in r and "probe#1" in r for r in result.reasons), result.reasons


def test_cell_values_in_rule_body_are_rejected():
    result = _check(_divergence(ensure="Select 营收金额 and every other spelling."))
    assert result.status == REJECTED
    assert any(r.startswith("ENSURE") and "营收金额" in r for r in result.reasons), result.reasons


def test_cell_values_in_trigger_example_fact_and_instance_are_allowed():
    d = _divergence(
        trigger="营收金额 of Beijing",
        example_usage="营收金额 vs 营业收入金额",
        fact="营收金额 is one of several spellings",
        instance="the agent kept only 营收金额",
    )
    assert _check(d).status == ACCEPTED, _check(d).reasons


def test_non_data_needs_none_of_the_new_fields():
    d = Divergence(index=1, divergence="flipped the subtraction", needed="2022 minus 2021", kind="non_data")
    result = _check(d)
    assert result.status == LOGGED and result.reasons == []


def test_missed_milestones_from_process_score():
    process = {"gpr": {"details": [
        {"key": "A", "achieved": False},
        {"key": "B", "achieved": True},
        {"key": "C", "achieved": False},
    ]}}
    assert missed_milestones(process) == {"A", "C"}
    assert missed_milestones(None) == set()
    assert missed_milestones({"gpr": {}}) == set()
