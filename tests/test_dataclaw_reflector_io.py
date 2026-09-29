"""Parsing the reflector's turns: one probe, or a final list of divergences."""

import pytest

from tkstore.dataclaw.reflector_io import parse_turn
from tkstore.dataclaw.scope import ColumnRef

OPS = "enterprise/company_operation_status.csv"

DATA_BLOCK = f"""DIVERGENCE: step 4 filtered targetName == 营收金额 only
NEEDED: filter by secondTargetNum and keep every targetName spelling
MISSING_DATA_UNDERSTANDING:
  SCOPE: multi_column
  TABLES:
  COLUMNS: {OPS}.secondTargetNum,
           {OPS}.targetName
  FACT: one secondTargetNum has several targetName spellings,
        each company uses one of them
CATEGORY: 同一指标有多个名字
EVIDENCE: probe#2 → Y_EC_5 营收金额 1004
EVIDENCE: probe#3 -> Y_EC_5 营业收入金额 2778
REPRODUCED: milestone "Top province by revenue" = "北京市" FROM probe#5
SEMANTIC_MATCH: 北京市 is the Chinese name of Beijing
REPRODUCED: milestone "Beijing total revenue" = 1.2e12 FROM probe#5
KIND: data
"""


def test_probe_turn():
    turn = parse_turn("PLAN: check spellings\n<probe>\ncut -d, -f4 ./database/x.csv | sort | uniq -c\n</probe>")
    assert turn.kind == "probe"
    assert turn.plan == "check spellings"
    assert turn.command == "cut -d, -f4 ./database/x.csv | sort | uniq -c"


def test_probe_command_code_fence_is_stripped():
    turn = parse_turn("PLAN: x\n<probe>\n```bash\nhead -1 a.csv\n```\n</probe>")
    assert turn.command == "head -1 a.csv"


@pytest.mark.parametrize(
    "text, reason",
    [
        ("PLAN: nothing to run", "neither"),
        ("<probe>a</probe>\n<probe>b</probe>", "one <probe>"),
        ("<probe>   </probe>", "empty"),
        ("<probe>ls</probe>\n<final></final>", "both"),
    ],
)
def test_invalid_turns_explain_why(text, reason):
    turn = parse_turn(text)
    assert turn.kind == "invalid"
    assert reason in turn.error


def test_final_with_no_divergence():
    turn = parse_turn("<final>\nNO_DATA_DIVERGENCE: the agent flipped the subtraction\n</final>")
    assert turn.kind == "final"
    assert turn.divergences == []
    assert turn.note == "the agent flipped the subtraction"


def test_data_divergence_fields():
    turn = parse_turn(f"<final>\n{DATA_BLOCK}</final>")
    (d,) = turn.divergences
    assert d.errors == []
    assert d.divergence == "step 4 filtered targetName == 营收金额 only"
    assert d.scope == "multi_column"
    assert d.tables == []
    assert d.columns == [ColumnRef(OPS, "secondTargetNum"), ColumnRef(OPS, "targetName")]
    assert d.fact == "one secondTargetNum has several targetName spellings,\neach company uses one of them"
    assert d.category == "同一指标有多个名字"
    assert [(e.probe, e.excerpt) for e in d.evidence] == [
        (2, "Y_EC_5 营收金额 1004"),
        (3, "Y_EC_5 营业收入金额 2778"),
    ]
    first, second = d.reproduced
    assert (first.key, first.value, first.probe) == ("Top province by revenue", "北京市", 5)
    assert first.semantic_match == "北京市 is the Chinese name of Beijing"
    assert (second.key, second.value, second.semantic_match) == ("Beijing total revenue", 1.2e12, None)
    assert d.kind == "data"


def test_multiple_blocks_and_non_data_block():
    text = f"<final>\n{DATA_BLOCK}\nDIVERGENCE: subtracted in the wrong order\nNEEDED: 2022 minus 2021\nKIND: non_data\n</final>"
    first, second = parse_turn(text).divergences
    assert first.kind == "data" and second.kind == "non_data"
    assert second.errors == [] and second.reproduced == []


def test_reproduced_with_list_and_dict_values():
    block = (
        "DIVERGENCE: d\nNEEDED: n\n"
        'REPRODUCED: milestone "Provinces" = ["北京市", "上海市"] FROM probe#1\n'
        'REPRODUCED: milestone "Counts" = {"Real Estate": 416} FROM probe#2\nKIND: data\n'
    )
    first, second = parse_turn(f"<final>{block}</final>").divergences[0].reproduced
    assert first.value == ["北京市", "上海市"]
    assert second.value == {"Real Estate": 416}


@pytest.mark.parametrize(
    "line, fragment",
    [
        ('REPRODUCED: milestone "k" = 北京市 FROM probe#1', "JSON"),
        ("REPRODUCED: k = 3 from probe 1", 'milestone "<key>"'),
        ("SEMANTIC_MATCH: dangling", "SEMANTIC_MATCH"),
        ("EVIDENCE: the output showed it", "probe#"),
        ("KIND: maybe", "KIND"),
        (f"  COLUMNS: {OPS}", "COLUMNS"),
        (f"  TABLES: {OPS}.value", "TABLES"),
    ],
)
def test_malformed_lines_become_block_errors(line, fragment):
    block = f"DIVERGENCE: d\nNEEDED: n\n{line}\n"
    if not line.startswith("KIND"):
        block += "KIND: data\n"
    (d,) = parse_turn(f"<final>{block}</final>").divergences
    assert any(fragment in e for e in d.errors), d.errors


def test_block_without_kind_is_an_error():
    (d,) = parse_turn("<final>DIVERGENCE: d\nNEEDED: n\n</final>").divergences
    assert any("KIND" in e for e in d.errors)


def test_text_outside_blocks_is_ignored():
    turn = parse_turn(f"Here is my answer.\n<final>\nSome preamble\n{DATA_BLOCK}</final>")
    assert len(turn.divergences) == 1 and turn.divergences[0].errors == []
