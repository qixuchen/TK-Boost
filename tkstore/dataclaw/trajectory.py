"""Parse and compress OpenClaw ``chat.jsonl`` trajectories.

Tool results are paired with their calls by ``toolCallId``: one assistant turn
often issues several parallel ``exec`` calls whose results may come back in any
order.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TIMESTAMP_PREFIX = re.compile(r"^\[\w{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}[^\]]*\]\s*")


@dataclass
class ToolCall:
    id: str
    name: str
    command: str
    result: str | None = None


@dataclass
class Step:
    index: int
    text: str = ""
    thinking: str = ""
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Trajectory:
    prompt: str
    steps: list[Step]
    final_answer: str


def _text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _messages(path: Path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "message":
            yield event.get("message") or {}


def parse_chat(path: Path) -> Trajectory:
    prompt: str | None = None
    steps: list[Step] = []
    calls_by_id: dict[str, ToolCall] = {}

    for message in _messages(path):
        role = message.get("role")
        content = message.get("content")
        if role == "user" and prompt is None:
            prompt = _TIMESTAMP_PREFIX.sub("", _text_parts(content).strip(), count=1)
        elif role == "assistant":
            step = Step(index=len(steps) + 1)
            texts, thoughts = [], []
            for part in content if isinstance(content, list) else []:
                kind = part.get("type") if isinstance(part, dict) else None
                if kind == "text":
                    texts.append(str(part.get("text") or ""))
                elif kind == "thinking":
                    thoughts.append(str(part.get("thinking") or ""))
                elif kind == "toolCall":
                    args = part.get("arguments")
                    if isinstance(args, dict) and "command" in args:
                        command = str(args["command"])
                    else:
                        command = json.dumps(args, ensure_ascii=False)
                    call = ToolCall(str(part.get("id") or ""), str(part.get("name") or "tool"), command)
                    step.calls.append(call)
                    calls_by_id[call.id] = call
            if isinstance(content, str):
                texts.append(content)
            step.text = "\n".join(t for t in texts if t).strip()
            step.thinking = "\n".join(t for t in thoughts if t).strip()
            steps.append(step)
        elif role in ("toolResult", "tool"):
            call = calls_by_id.get(str(message.get("toolCallId") or ""))
            if call is not None:
                call.result = _text_parts(content)

    final_answer = next((s.text for s in reversed(steps) if s.text), "")
    return Trajectory(prompt=prompt or "", steps=steps, final_answer=final_answer)


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n...[{omitted} chars omitted]...\n{text[len(text) - tail:]}"


def compress(
    trajectory: Trajectory,
    *,
    max_output_chars: int = 2000,
    max_thinking_chars: int = 5000,
    keep_thinking: bool = True,
) -> str:
    """Render the trajectory as numbered steps; CALL numbers are global so they can be cited."""
    lines: list[str] = []
    call_number = 0
    for step in trajectory.steps:
        lines.append(f"[step {step.index}]")
        if keep_thinking and step.thinking:
            lines.append(f"THINKING: {truncate_middle(step.thinking, max_thinking_chars)}")
        if step.text:
            lines.append(f"TEXT: {step.text}")
        for call in step.calls:
            call_number += 1
            lines.append(f"CALL #{call_number} {call.name}: {call.command}")
            if call.result is None:
                lines.append(f"OUTPUT #{call_number}: (no result)")
            else:
                lines.append(f"OUTPUT #{call_number}:\n{truncate_middle(call.result, max_output_chars)}")
        lines.append("")
    lines.append(f"FINAL ANSWER: {trajectory.final_answer}")
    return "\n".join(lines) + "\n"


def length_stats(lengths: list[int]) -> dict[str, int]:
    """Nearest-rank min / median / p95 / max."""
    if not lengths:
        return {"min": 0, "median": 0, "p95": 0, "max": 0}
    ordered = sorted(lengths)

    def rank(q: float) -> int:
        return ordered[max(math.ceil(q * len(ordered)) - 1, 0)]

    return {"min": ordered[0], "median": rank(0.5), "p95": rank(0.95), "max": ordered[-1]}
