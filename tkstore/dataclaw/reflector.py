"""The reflector loop: probe the data, then submit gated divergences with their rules.

A data divergence that passes the deterministic gates goes to one LLM call, the
generality judge; a rejection is sent back like a gate rejection.
The LLMs are injected as callables (messages -> reply text or ``LLMReply``), so
the loop can be tested with scripted models; ``litellm_llm`` is the real one.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Callable

from .catalog import Catalog
from .devset import LoadedRun
from .gates import ACCEPTED, LOGGED, REJECTED, GateResult, check_divergence, missed_milestones
from .milestones import find_milestone
from .probe import ProbeRecord, ProbeSession, numbered_view
from .reflector_io import Divergence, Evidence, Reproduction, parse_turn
from .trajectory import compress

LLM = Callable[[list[dict[str, str]]], "str | LLMReply"]
PROMPT_PATH = Path(__file__).with_name("prompts") / "reflector.md"
JUDGE_PROMPT_PATH = Path(__file__).with_name("prompts") / "reflector_judge.md"
REPRODUCTION_PROMPT_PATH = Path(__file__).with_name("prompts") / "reflector_judge_reproduction.md"
FIELDS_PATH = Path(__file__).with_name("prompts") / "divergence_fields.md"
GENERALITY_JUDGE_FIELDS = (
    "DIVERGENCE", "NEEDED", "BASIS", "BASIS_QUOTE", "INSTANCE", "SCOPE", "TABLES", "COLUMNS", "FACT",
    "CATEGORY", "EVIDENCE", "GENERALITY", "ENSURE", "WHEN_TO_CHECK", "TRIGGER", "CONTEXT", "EXAMPLE_USAGE",
)
REPRODUCTION_JUDGE_FIELDS = ("DIVERGENCE", "NEEDED", "INSTANCE", "REPRODUCED", "SEMANTIC_MATCH")
_FIELD_ENTRY = re.compile(r"^- ([A-Z_]+): ", re.MULTILINE)
JUDGE_ERROR = "judge_error"
JUDGE_ATTEMPTS = 2
GENERALITY_VOTES = 3
JUDGE_PROBE_CHARS = 8000
EXTRA_TURNS = 3
_VERDICT = re.compile(r"^\s*VERDICT:\s*(accept|reject)\b", re.IGNORECASE | re.MULTILINE)
_REASON = re.compile(r"^\s*REASON:\s*(.*)", re.MULTILINE | re.DOTALL)
_REPRO_BLOCK = re.compile(r"^\s*REPRODUCED\s+(\d+)\s*$", re.MULTILINE)
_REPRO_FIELD = re.compile(r"^\s*(PROBE_QUANTITY|MILESTONE_QUANTITY|VERDICT|REASON):\s*(.*)$", re.MULTILINE)
EMPTY_REPLY_NUDGE = (
    "EMPTY REPLY: your previous reply was cut off before any text. Do not deliberate at length; "
    "reply now with one short PLAN and one <probe>, or with your <final>."
)
TIME_BUDGET_NOTE = "TIME BUDGET: this reflection has used up its time budget; send your <final> now."


@dataclass
class ReflectorConfig:
    max_probes: int = 20
    max_finals: int = 5
    max_format_retries: int = 3
    max_output_chars: int = 2000
    max_thinking_chars: int = 5000
    max_consecutive_empty: int = 2
    run_time_budget_s: float | None = None

    @property
    def max_turns(self) -> int:
        """Hard cap on LLM calls, leaving a few turns for invalid replies."""
        return self.max_probes + self.max_finals + self.max_format_retries + EXTRA_TURNS


@dataclass
class ReflectionResult:
    accepted: list[GateResult] = field(default_factory=list)
    rejected: list[GateResult] = field(default_factory=list)
    logged: list[GateResult] = field(default_factory=list)
    judge_errors: list[GateResult] = field(default_factory=list)
    probes: list[ProbeRecord] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    probes_used: int = 0
    finals: int = 0
    format_retries: int = 0
    ever_rejected: bool = False
    stop_reason: str = ""
    error: str = ""
    llm_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LLMReply:
    text: str
    finish_reason: str | None = None
    timed_out: bool = False
    elapsed_s: float = 0.0


def litellm_llm(
    model: str,
    *,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    disable_thinking: bool = False,
    call_timeout_s: float | None = None,
    clock: Callable[[], float] | None = None,
) -> LLM:
    """Streaming completion; the gateway drops non-streaming requests idle for ~3 minutes.

    Reasoning controls go through ``extra_body`` so LiteLLM passes them to the
    OpenAI-compatible gateway unchecked. ``call_timeout_s`` is enforced here
    because HTTP timeouts only cover the gap between chunks, and a model that
    keeps reasoning keeps sending chunks.
    """
    import litellm

    tick = clock or time.monotonic
    options: dict[str, Any] = {}
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    extra: dict[str, Any] = {}
    if reasoning_effort:
        extra["reasoning_effort"] = reasoning_effort
    if disable_thinking:
        extra["thinking"] = {"type": "disabled"}
    if extra:
        options["extra_body"] = extra

    def call(messages: list[dict[str, str]]) -> LLMReply:
        start = tick()
        stream = litellm.completion(model=model, messages=messages, stream=True, **options)
        parts: list[str] = []
        finish = None
        for chunk in stream:
            choice = chunk.choices[0]
            parts.append(choice.delta.content or "")
            finish = getattr(choice, "finish_reason", None) or finish
            now = tick()
            if call_timeout_s is not None and now - start > call_timeout_s:
                close = getattr(stream, "close", None)
                if close:
                    close()
                return LLMReply("".join(parts), "timeout", True, now - start)
        return LLMReply("".join(parts), finish, False, tick() - start)

    return call


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _process(loaded: LoadedRun) -> dict[str, Any]:
    return (loaded.process_score or {}).get("gpr") or {}


def _steps(gold: dict[str, Any]) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(gold.get("steps") or [], 1))


def _judged(loaded: LoadedRun) -> str:
    return "\n".join(
        f"- {d['key']} (expected {_json(d.get('expected'))}): "
        f"{'achieved' if d.get('achieved') else 'MISSED'} - {d.get('reason', '')}"
        for d in _process(loaded).get("details") or []
    )


def _files(catalog: Catalog) -> str:
    return "\n".join(
        f"{name}: {', '.join(columns)}" if columns else f"{name}: (JSON file; reference it under TABLES)"
        for name, columns in catalog.headers.items()
    )


def _user_message(loaded: LoadedRun, catalog: Catalog, config: ReflectorConfig) -> str:
    gold = loaded.gold
    process = _process(loaded)
    missed = missed_milestones(loaded.process_score)
    milestones = gold.get("milestone") or {}

    steps = _steps(gold)
    judged = _judged(loaded)
    missed_lines = "\n".join(
        f"- {key} = {_json(value)}" for key, value in milestones.items() if key in missed
    ) or "- (none)"
    files = _files(catalog)
    trajectory = compress(
        loaded.trajectory,
        max_output_chars=config.max_output_chars,
        max_thinking_chars=config.max_thinking_chars,
    )
    return "\n\n".join([
        f"## Task given to the agent\n{loaded.prompt}",
        f"## Agent trajectory (compressed; A<n> numbers the agent's commands, which you cannot cite)\n{trajectory}",
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
        max_probes=config.max_probes, max_finals=config.max_finals, max_format_retries=config.max_format_retries
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _user_message(loaded, catalog, config)},
    ]


def _verdict_message(results: list[GateResult], *, probes_left: int, finals_left: int, format_left: int) -> str:
    lines = ["HARNESS VERDICT:"]
    for r in results:
        lines.append(f"divergence {r.divergence.index}: {r.status.upper()}")
        lines.extend(f"  - {reason}" for reason in r.reasons)
    lines.append(
        "Accepted divergences are kept. You may run more probes before resubmitting "
        f"({probes_left} probe(s) left), then resubmit a <final> containing only the rejected "
        f"divergences you have fixed. You have {finals_left} <final> submission(s) left; a <final> "
        f"rejected only for format errors does not use one ({format_left} format retries left)."
    )
    return "\n".join(lines)


def _call(
    llm: LLM, messages: list[dict[str, str]], stage: str, calls: list[dict[str, Any]], divergence: int | None = None
) -> LLMReply:
    """Call the LLM and log its stats; a judge call also keeps its divergence index and reply text."""
    raw = llm(messages)
    reply = raw if isinstance(raw, LLMReply) else LLMReply(raw)
    entry = {
        "stage": stage,
        "finish_reason": reply.finish_reason,
        "timed_out": reply.timed_out,
        "elapsed_s": reply.elapsed_s,
        "text_chars": len(reply.text),
    }
    if divergence is not None:
        entry.update(divergence=divergence, text=reply.text)
    calls.append(entry)
    return reply


def _pointer(p: Evidence | Reproduction) -> str:
    span = f"L{p.start}" if p.start == p.end else f"L{p.start}-L{p.end}"
    return f"P{p.probe}:{span}"


def _divergence_text(d: Divergence) -> str:
    lines = [
        f"DIVERGENCE: {d.divergence}",
        f"NEEDED: {d.needed}",
        f"BASIS: {d.basis}",
    ]
    if d.basis_quote:
        lines.append(f"BASIS_QUOTE: {d.basis_quote}")
    lines += [
        f"INSTANCE: {d.instance}",
        f"SCOPE: {d.scope}",
        f"TABLES: {', '.join(d.tables)}",
        f"COLUMNS: {', '.join(f'{c.file}.{c.column}' for c in d.columns)}",
        f"FACT: {d.fact}",
        f"CATEGORY: {d.category}",
    ]
    lines += [f"EVIDENCE: {_pointer(e)} → {e.excerpt}" for e in d.evidence]
    lines += [f"GENERALITY: {_pointer(g)} → {g.excerpt}" for g in d.generality]
    lines += [
        f"ENSURE: {d.ensure}",
        f"WHEN_TO_CHECK: {d.when_to_check}",
        f"TRIGGER: {d.trigger}",
        f"CONTEXT: {d.context}",
        f"EXAMPLE_USAGE: {d.example_usage}",
    ]
    return "\n".join(lines)


def _probe_text(record: ProbeRecord) -> str:
    output = numbered_view(record.output, JUDGE_PROBE_CHARS)[0] if record.output else "(no output)"
    return f"P{record.number}: {record.command}\n{output}"


def _gold_sections(loaded: LoadedRun, catalog: Catalog) -> list[str]:
    gold = loaded.gold
    return [
        f"## Task given to the agent\n{loaded.prompt}",
        f"## Gold answer\n{_json(gold.get('answer'))}",
        f"## Gold steps\n{_steps(gold)}",
        f"## Gold milestones\n{_json(gold.get('milestone') or {})}",
        f"## Files under ./database/ and their columns\n{_files(catalog)}",
    ]


def _probes_section(probes: list[ProbeRecord]) -> str:
    return "## Every probe the reflector ran (command and line-numbered output)\n" + "\n\n".join(
        _probe_text(r) for r in probes
    )


def field_meanings(names: tuple[str, ...]) -> str:
    """The entries of divergence_fields.md for ``names``, in that order."""
    text = FIELDS_PATH.read_text(encoding="utf-8")
    heads = list(_FIELD_ENTRY.finditer(text))
    entries = {
        head.group(1): text[head.start():(heads[i + 1].start() if i + 1 < len(heads) else len(text))].rstrip()
        for i, head in enumerate(heads)
    }
    return "\n".join(entries[name] for name in names)


def _judge_system(path: Path, names: tuple[str, ...]) -> str:
    return Template(path.read_text(encoding="utf-8")).substitute(fields=field_meanings(names))


def build_judge_messages(
    d: Divergence, probes: list[ProbeRecord], loaded: LoadedRun, catalog: Catalog
) -> list[dict[str, str]]:
    user = "\n\n".join([
        *_gold_sections(loaded, catalog),
        f"## Divergence and rule\n{_divergence_text(d)}",
        _probes_section(probes),
    ])
    return [
        {"role": "system", "content": _judge_system(JUDGE_PROMPT_PATH, GENERALITY_JUDGE_FIELDS)},
        {"role": "user", "content": user},
    ]


def _claim_text(i: int, r: Reproduction, milestones: dict[str, Any]) -> str:
    key, expected = find_milestone(milestones, r.key) or (r.key, None)
    line = f'REPRODUCED {i}: milestone "{key}" (gold {_json(expected)}) = {_json(r.value)} FROM {_pointer(r)} → {r.excerpt}'
    return f"{line}\n  SEMANTIC_MATCH: {r.semantic_match}" if r.semantic_match else line


def build_reproduction_messages(
    d: Divergence, probes: list[ProbeRecord], loaded: LoadedRun, catalog: Catalog
) -> list[dict[str, str]]:
    milestones = loaded.gold.get("milestone") or {}
    user = "\n\n".join([
        *_gold_sections(loaded, catalog),
        f"## Process judge on each milestone\n{_judged(loaded)}",
        f"## Divergence\nDIVERGENCE: {d.divergence}\nNEEDED: {d.needed}\nINSTANCE: {d.instance}",
        "## Claims to judge (cited lines filled in by the harness)\n"
        + "\n".join(_claim_text(i, r, milestones) for i, r in enumerate(d.reproduced, 1)),
        _probes_section(probes),
    ])
    return [
        {"role": "system", "content": _judge_system(REPRODUCTION_PROMPT_PATH, REPRODUCTION_JUDGE_FIELDS)},
        {"role": "user", "content": user},
    ]


@dataclass(frozen=True)
class ReproductionVerdict:
    probe_quantity: str
    milestone_quantity: str
    verdict: str
    reason: str


def parse_reproduction_judge(text: str, count: int) -> list[ReproductionVerdict] | None:
    """One verdict per REPRODUCED claim, in order; None unless every claim has a match/mismatch VERDICT."""
    text = text or ""
    heads = list(_REPRO_BLOCK.finditer(text))
    blocks: dict[int, dict[str, str]] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        blocks[int(head.group(1))] = {k: v.strip() for k, v in _REPRO_FIELD.findall(text[head.end():end])}
    verdicts = []
    for n in range(1, count + 1):
        fields = blocks.get(n, {})
        verdict = fields.get("VERDICT", "").rstrip(".").lower()
        if verdict not in ("match", "mismatch"):
            return None
        verdicts.append(ReproductionVerdict(
            fields.get("PROBE_QUANTITY", ""), fields.get("MILESTONE_QUANTITY", ""), verdict, fields.get("REASON", "")
        ))
    return verdicts


def parse_judge(text: str) -> tuple[str | None, str]:
    """Return ("accept" | "reject" | None, reason); None when the reply has no VERDICT line."""
    verdict = _VERDICT.search(text or "")
    reason = _REASON.search(text or "")
    return (verdict.group(1).lower() if verdict else None, reason.group(1).strip() if reason else "")


def _judge_reproduction(
    v: GateResult, llm: LLM, session: ProbeSession, result: ReflectionResult, loaded: LoadedRun, catalog: Catalog
) -> GateResult:
    """Reproduction judge on a divergence that passed the deterministic gates; retried once."""
    claims = v.divergence.reproduced
    if not claims:
        return v
    messages = build_reproduction_messages(v.divergence, session.records, loaded, catalog)
    reply = None
    for _ in range(JUDGE_ATTEMPTS):
        reply = _call(llm, messages, "judge_reproduction", result.llm_calls, v.divergence.index)
        verdicts = parse_reproduction_judge(reply.text, len(claims))
        if verdicts is not None:
            reasons = [f"REPRODUCTION JUDGE: {x.reason}" for x in verdicts if x.verdict == "mismatch"]
            return GateResult(v.divergence, REJECTED, reasons) if reasons else v
    return GateResult(
        v.divergence, JUDGE_ERROR, [f"reproduction judge reply had no verdict per claim: {reply.text[:200]!r}"]
    )


def _judge(
    v: GateResult, llm: LLM, session: ProbeSession, result: ReflectionResult, loaded: LoadedRun, catalog: Catalog
) -> GateResult:
    """Generality judge on a divergence that passed the reproduction judge: majority of up to
    GENERALITY_VOTES votes, stopping once one side has a majority; an unparseable vote is retried once."""
    messages = build_judge_messages(v.divergence, session.records, loaded, catalog)
    accepts, rejects = 0, []
    for _ in range(GENERALITY_VOTES):
        for _ in range(JUDGE_ATTEMPTS):
            reply = _call(llm, messages, "judge_generality", result.llm_calls, v.divergence.index)
            verdict, reason = parse_judge(reply.text)
            if verdict is not None:
                break
        else:
            return GateResult(v.divergence, JUDGE_ERROR, [f"judge reply had no VERDICT line: {reply.text[:200]!r}"])
        if verdict == "accept":
            accepts += 1
        else:
            rejects.append(f"GENERALITY JUDGE: {reason}")
        if 2 * accepts > GENERALITY_VOTES:
            return v
        if 2 * len(rejects) > GENERALITY_VOTES:
            return GateResult(v.divergence, REJECTED, rejects)
    raise AssertionError("GENERALITY_VOTES must be odd")


def _identity(result: GateResult) -> tuple:
    d = result.divergence
    return (d.fact.strip(), tuple(d.tables), tuple(d.columns))


def _handle_turn(
    text: str,
    result: ReflectionResult,
    *,
    session: ProbeSession,
    catalog: Catalog,
    config: ReflectorConfig,
    milestones: dict[str, Any],
    missed: set[str],
    seen: set[tuple],
    loaded: LoadedRun,
    judge_llm: LLM,
) -> str | None:
    """Act on one non-empty reply; return the feedback, or None once the run is over."""
    turn = parse_turn(text)
    if turn.kind == "invalid":
        return f"FORMAT ERROR: {turn.error}. Send one <probe> or one <final>."
    if turn.kind == "probe":
        if result.probes_used >= config.max_probes:
            return "The probe budget is used up; send your <final> now."
        record = session.run(turn.command)
        result.probes_used += 1
        return session.llm_view(record)

    outputs = {r.number: session.lines(r) for r in session.records}
    verdicts = [
        check_divergence(
            d, probes=outputs, catalog=catalog, milestones=milestones, missed=missed, task_prompt=loaded.prompt,
            agent_calls=sum(len(step.calls) for step in loaded.trajectory.steps),
        )
        for d in turn.divergences
    ]
    for i, v in enumerate(verdicts):
        if v.status == ACCEPTED and _identity(v) not in seen:
            v = _judge_reproduction(v, judge_llm, session, result, loaded, catalog)
            if v.status == ACCEPTED:
                v = _judge(v, judge_llm, session, result, loaded, catalog)
            verdicts[i] = v
            if v.status == ACCEPTED:
                seen.add(_identity(v))
                result.accepted.append(v)
            elif v.status == JUDGE_ERROR:
                result.judge_errors.append(v)
        elif v.status == LOGGED:
            result.logged.append(v)
    result.rejected = [v for v in verdicts if v.status == REJECTED]
    if result.rejected and all(v.structural for v in result.rejected):
        result.format_retries += 1
    else:
        result.finals += 1
    if not result.rejected:
        gave_up = result.ever_rejected and not any(d.kind == "data" for d in turn.divergences)
        result.stop_reason = "abandoned_after_reject" if gave_up else "done"
        return None
    result.ever_rejected = True
    if result.finals >= config.max_finals:
        result.stop_reason = "final_budget"
        return None
    if result.format_retries >= config.max_format_retries:
        result.stop_reason = "format_budget"
        return None
    return _verdict_message(
        verdicts,
        probes_left=config.max_probes - result.probes_used,
        finals_left=config.max_finals - result.finals,
        format_left=config.max_format_retries - result.format_retries,
    )


def reflect(
    loaded: LoadedRun,
    *,
    llm: LLM,
    session: ProbeSession,
    catalog: Catalog,
    config: ReflectorConfig,
    clock: Callable[[], float] | None = None,
    judge_llm: LLM | None = None,
) -> ReflectionResult:
    """Run one reflection; errors and interrupts end it but keep what was collected."""
    tick = clock or time.monotonic
    result = ReflectionResult(messages=build_messages(loaded, catalog, config))
    context = dict(
        session=session,
        catalog=catalog,
        config=config,
        milestones=loaded.gold.get("milestone") or {},
        missed=missed_milestones(loaded.process_score),
        seen=set(),
        loaded=loaded,
        judge_llm=judge_llm or llm,
    )
    start = tick()
    empties = 0
    warned = False

    try:
        for _ in range(config.max_turns):
            reply = _call(llm, result.messages, "reflect", result.llm_calls)
            result.messages.append({"role": "assistant", "content": reply.text})

            if not reply.text.strip():
                empties += 1
                if empties >= config.max_consecutive_empty:
                    result.stop_reason = "empty_reply"
                    break
                feedback = EMPTY_REPLY_NUDGE
            else:
                empties = 0
                feedback = _handle_turn(reply.text, result, **context)
                if feedback is None:
                    break

            if config.run_time_budget_s is not None and tick() - start > config.run_time_budget_s:
                if warned:
                    result.stop_reason = "time_budget"
                    break
                warned = True
                feedback = f"{feedback}\n\n{TIME_BUDGET_NOTE}"
            result.messages.append({"role": "user", "content": feedback})
        else:
            result.stop_reason = "turn_budget"
    except KeyboardInterrupt:
        result.stop_reason = "interrupted"
    except Exception as exc:
        result.stop_reason = "error"
        result.error = f"{type(exc).__name__}: {exc}"

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
        "error": result.error,
        "llm_calls": result.llm_calls,
        "probes_used": result.probes_used,
        "finals": result.finals,
        "format_retries": result.format_retries,
        "accepted": [asdict(r) for r in result.accepted],
        "rejected": [asdict(r) for r in result.rejected],
        "judge_errors": [asdict(r) for r in result.judge_errors],
        "logged": [asdict(r) for r in result.logged],
        "probes": [asdict(p) for p in result.probes],
        "messages": result.messages,
    }
