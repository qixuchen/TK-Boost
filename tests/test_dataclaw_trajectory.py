"""Parsing and compressing OpenClaw chat.jsonl trajectories."""

import json

import pytest

from tkstore.dataclaw.trajectory import (
    Step,
    Trajectory,
    compress,
    length_stats,
    parse_chat,
    truncate_middle,
)


def _message(role, content, **extra):
    return {"type": "message", "message": {"role": role, "content": content, **extra}}


def _call(call_id, command):
    return {"type": "toolCall", "id": call_id, "name": "exec", "arguments": {"command": command}}


def _result(call_id, text):
    return _message("toolResult", [{"type": "text", "text": text}], toolCallId=call_id)


@pytest.fixture
def chat_path(tmp_path):
    events = [
        {"type": "session", "cwd": "/root/.openclaw/workspace"},
        _message("user", [{"type": "text", "text": "[Fri 2026-07-10 07:36 UTC] Which province leads?"}]),
        _message(
            "assistant",
            [
                {"type": "thinking", "thinking": "Look at the files first."},
                _call("a", "head -3 a.csv"),
                _call("b", "grep 半导体 b.csv"),
            ],
        ),
        _result("b", "B-OUTPUT"),
        _result("a", "A-OUTPUT"),
        _message("assistant", [{"type": "text", "text": "Checking more."}, _call("c", "wc -l c.csv")]),
        _message("assistant", [{"type": "text", "text": "Guangdong"}]),
    ]
    path = tmp_path / "chat.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    path.write_text("\n".join(lines[:2] + ["", "{not json"] + lines[2:]) + "\n", encoding="utf-8")
    return path


def test_prompt_has_timestamp_prefix_stripped(chat_path):
    assert parse_chat(chat_path).prompt == "Which province leads?"


def test_prompt_without_timestamp_is_kept_whole(tmp_path):
    path = tmp_path / "chat.jsonl"
    path.write_text(
        json.dumps(_message("user", [{"type": "text", "text": "[TRIBAL_KNOWLEDGE] rules"}])) + "\n",
        encoding="utf-8",
    )
    assert parse_chat(path).prompt == "[TRIBAL_KNOWLEDGE] rules"


def test_one_step_per_assistant_message(chat_path):
    steps = parse_chat(chat_path).steps
    assert [s.index for s in steps] == [1, 2, 3]
    assert steps[0].thinking == "Look at the files first."
    assert steps[1].text == "Checking more."


def test_tool_results_are_paired_by_call_id_not_order(chat_path):
    calls = parse_chat(chat_path).steps[0].calls
    assert [(c.id, c.command, c.result) for c in calls] == [
        ("a", "head -3 a.csv", "A-OUTPUT"),
        ("b", "grep 半导体 b.csv", "B-OUTPUT"),
    ]


def test_call_without_result_keeps_none(chat_path):
    (call,) = parse_chat(chat_path).steps[1].calls
    assert call.command == "wc -l c.csv" and call.result is None


def test_non_command_arguments_are_serialized(tmp_path):
    path = tmp_path / "chat.jsonl"
    call = {"type": "toolCall", "id": "r", "name": "read", "arguments": {"path": "x.csv"}}
    path.write_text(json.dumps(_message("assistant", [call])) + "\n", encoding="utf-8")
    (parsed,) = parse_chat(path).steps[0].calls
    assert parsed.name == "read" and json.loads(parsed.command) == {"path": "x.csv"}


def test_final_answer_is_last_assistant_text(chat_path):
    assert parse_chat(chat_path).final_answer == "Guangdong"


def test_truncate_middle_keeps_head_and_tail():
    text = "H" * 10 + "M" * 80 + "T" * 10
    out = truncate_middle(text, 20)
    assert out.startswith("H" * 10) and out.endswith("T" * 10)
    assert "80 chars omitted" in out
    assert truncate_middle("short", 20) == "short"


def test_compress_numbers_calls_globally_and_truncates_outputs(chat_path):
    text = compress(parse_chat(chat_path), max_output_chars=4, max_thinking_chars=500)
    assert "CALL #1 exec: head -3 a.csv" in text
    assert "CALL #2 exec: grep 半导体 b.csv" in text
    assert "CALL #3 exec: wc -l c.csv" in text
    assert "(no result)" in text
    assert "A-OUTPUT" not in text and "chars omitted" in text
    assert "THINKING: Look at the files first." in text
    assert text.rstrip().endswith("FINAL ANSWER: Guangdong")


def test_compress_can_drop_or_truncate_thinking(chat_path):
    trajectory = parse_chat(chat_path)
    assert "THINKING" not in compress(trajectory, keep_thinking=False)
    shortened = compress(trajectory, max_thinking_chars=6)
    assert "Look at the files first." not in shortened
    assert "THINKING: Loo" in shortened


def test_compress_keeps_up_to_5000_thinking_chars_by_default():
    def with_thinking(n):
        return Trajectory(prompt="", steps=[Step(index=1, thinking="t" * n)], final_answer="")

    assert "t" * 5000 in compress(with_thinking(5000))
    assert "chars omitted" in compress(with_thinking(5001))


def test_length_stats():
    assert length_stats([5, 1, 3, 2, 4]) == {"min": 1, "median": 3, "p95": 5, "max": 5}
    assert length_stats([]) == {"min": 0, "median": 0, "p95": 0, "max": 0}
