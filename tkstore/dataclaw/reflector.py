"""The reflector loop: probe the data, then submit gated divergences.

The LLM is injected as a callable (messages -> reply text), so the loop and the
gates can be tested with a scripted model; ``litellm_llm`` is the real one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Callable

from .catalog import Catalog
from .devset import LoadedRun
from .gates import ACCEPTED, LOGGED, GateResult, check_divergence, missed_milestones
from .probe import ProbeRecord, ProbeSession
from .reflector_io import parse_turn
from .trajectory import compress

LLM = Callable[[list[dict[str, str]]], str]
PROMPT_PATH = Path(__file__).with_name("prompts") / "reflector.md"
EXTRA_TURNS = 3


@dataclass
class ReflectorConfig:
    max_probes: int = 20
    max_finals: int = 3
    max_output_chars: int = 2000
    max_thinking_chars: int = 5000

    @property
    def max_turns(self) -> int:
        """Hard cap on LLM calls, leaving a few turns for invalid replies."""
        return self.max_probes + self.max_finals + EXTRA_TURNS


@dataclass
class ReflectionResult:
    accepted: list[GateResult] = field(default_factory=list)
    rejected: list[GateResult] = field(default_factory=list)
    logged: list[GateResult] = field(default_factory=list)
    probes: list[ProbeRecord] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    probes_used: int = 0
    finals: int = 0
    stop_reason: str = ""


def litellm_llm(model: str) -> LLM:
    import litellm

    def call(messages: list[dict[str, str]]) -> str:
        response = litellm.completion(model=model, messages=messages)
        return response.choices[0].message.content or ""

    return call


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _user_message(loaded: LoadedRun, catalog: Catalog, config: ReflectorConfig) -> str:
    gold = loaded.gold
    process = (loaded.process_score or {}).get("gpr") or {}
    details = process.get("details") or []
    missed = missed_milestones(loaded.process_score)
    milestones = gold.get("milestone") or {}

    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(gold.get("steps") or [], 1))
    judged = "\n".join(
        f"- {d['key']} (expected {_json(d.get('expected'))}): "
        f"{'achieved' if d.get('achieved') else 'MISSED'} - {d.get('reason', '')}"
        for d in details
    )
    missed_lines = "\n".join(
        f"- {key} = {_json(value)}" for key, value in milestones.items() if key in missed
    ) or "- (none)"
    files = "\n".join(
        f"{name}: {', '.join(columns)}" if columns else f"{name}: (JSON file; reference it under TABLES)"
        for name, columns in catalog.headers.items()
    )
    trajectory = compress(
        loaded.trajectory,
        max_output_chars=config.max_output_chars,
        max_thinking_chars=config.max_thinking_chars,
    )
    return "\n\n".join([
        f"## Task given to the agent\n{loaded.prompt}",
        f"## Agent trajectory (compressed; CALL #n numbers the agent's commands)\n{trajectory}",
        f"## Outcome judge\n{loaded.score.get('notes', '')}",
        "## Process judge\n"
        f"break_point: {process.get('break_point')}\n"
        f"chain_summary: {process.get('chain_summary', '')}\n{judged}",
        f"## Gold answer\n{_json(gold.get('answer'))}",
        f"## Gold steps\n{steps}",
        f"## Milestones you may reproduce (the agent missed them)\n{missed_lines}",
        f"## Files under ./database/ and their columns\n{files}",
    ])


def build_messages(loaded: LoadedRun, catalog: Catalog, config: ReflectorConfig) -> list[dict[str, str]]:
    system = Template(PROMPT_PATH.read_text(encoding="utf-8")).substitute(
        max_probes=config.max_probes, max_finals=config.max_finals
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _user_message(loaded, catalog, config)},
    ]


def _verdict_message(results: list[GateResult], finals_left: int) -> str:
    lines = ["HARNESS VERDICT:"]
    for r in results:
        lines.append(f"divergence {r.divergence.index}: {r.status.upper()}")
        lines.extend(f"  - {reason}" for reason in r.reasons)
    lines.append(
        "Accepted divergences are kept. Resubmit a <final> containing only the rejected "
        f"divergences you have fixed; you have {finals_left} <final> submission(s) left."
    )
    return "\n".join(lines)


def _identity(result: GateResult) -> tuple:
    d = result.divergence
    return (d.fact.strip(), tuple(d.tables), tuple(d.columns))


def reflect(
    loaded: LoadedRun,
    *,
    llm: LLM,
    session: ProbeSession,
    catalog: Catalog,
    config: ReflectorConfig,
) -> ReflectionResult:
    result = ReflectionResult(messages=build_messages(loaded, catalog, config))
    milestones = loaded.gold.get("milestone") or {}
    missed = missed_milestones(loaded.process_score)
    seen: set[tuple] = set()

    for _ in range(config.max_turns):
        reply = llm(result.messages)
        result.messages.append({"role": "assistant", "content": reply})
        turn = parse_turn(reply)

        if turn.kind == "invalid":
            feedback = f"FORMAT ERROR: {turn.error}. Send one <probe> or one <final>."
        elif turn.kind == "probe":
            if result.probes_used >= config.max_probes:
                feedback = "The probe budget is used up; send your <final> now."
            else:
                record = session.run(turn.command)
                result.probes_used += 1
                feedback = session.llm_view(record)
        else:
            result.finals += 1
            outputs = {r.number: r.output for r in session.records}
            verdicts = [
                check_divergence(d, probes=outputs, catalog=catalog, milestones=milestones, missed=missed)
                for d in turn.divergences
            ]
            for v in verdicts:
                if v.status == ACCEPTED and _identity(v) not in seen:
                    seen.add(_identity(v))
                    result.accepted.append(v)
                elif v.status == LOGGED:
                    result.logged.append(v)
            result.rejected = [v for v in verdicts if v.status not in (ACCEPTED, LOGGED)]
            if not result.rejected:
                result.stop_reason = "done"
                break
            if result.finals >= config.max_finals:
                result.stop_reason = "final_budget"
                break
            feedback = _verdict_message(verdicts, config.max_finals - result.finals)
        result.messages.append({"role": "user", "content": feedback})
    else:
        result.stop_reason = "turn_budget"

    result.probes = list(session.records)
    return result


def to_record(loaded: LoadedRun, result: ReflectionResult) -> dict[str, Any]:
    run = loaded.run
    return {
        "task_id": run.task_id,
        "run_dir": run.run_dir.name,
        "category": run.category,
        "level": run.level,
        "stop_reason": result.stop_reason,
        "probes_used": result.probes_used,
        "finals": result.finals,
        "accepted": [asdict(r) for r in result.accepted],
        "rejected": [asdict(r) for r in result.rejected],
        "logged": [asdict(r) for r in result.logged],
        "probes": [asdict(p) for p in result.probes],
        "messages": result.messages,
    }
