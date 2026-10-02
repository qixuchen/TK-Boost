"""Shell probes for the reflector, run in the same image as the OpenClaw agent.

The container mirrors DataClaw's layout (``/tmp_workspace/database`` with
``/root/.openclaw/workspace`` linked to it) so the agent's own commands can be
replayed verbatim; unlike DataClaw it mounts the data read-only instead of
copying it, and has no network.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

WORKSPACE = "/tmp_workspace"
TIMEOUT_EXIT_CODE = 124
HOST_TIMEOUT_GRACE = 10


class ExecResult(NamedTuple):
    output: str
    exit_code: int | None
    timed_out: bool


@dataclass
class ProbeRecord:
    number: int
    command: str
    output: str
    exit_code: int | None
    timed_out: bool


@dataclass(frozen=True)
class ProbeLines:
    """A probe's output lines and the 1-based numbers of the lines the reflector was shown."""

    lines: tuple[str, ...]
    visible: frozenset[int]

    @classmethod
    def full(cls, output: str) -> "ProbeLines":
        lines = tuple(output.splitlines())
        return cls(lines, frozenset(range(1, len(lines) + 1)))


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    keep = max(cap - 25, 1)
    return f"{text[:keep]}...[{len(text) - keep} chars cut]"


def numbered_view(output: str, limit: int) -> tuple[str, frozenset[int]]:
    """Number the lines ``L<k>| ``; past ``limit`` chars keep whole head and tail lines.

    A line longer than half the limit is cut but stays citable; lines dropped
    from the middle are not, so they are left out of the returned visible set.
    """
    lines = output.splitlines()
    cap = max(limit // 2, 1)
    rendered = [_cut(f"L{i}| {line}", cap) for i, line in enumerate(lines, 1)]
    everything = frozenset(range(1, len(lines) + 1))
    if sum(len(r) + 1 for r in rendered) <= limit + 1:
        return "\n".join(rendered), everything

    head: list[int] = []
    used = 0
    for i, text in enumerate(rendered):
        if head and used + len(text) + 1 > cap:
            break
        head.append(i)
        used += len(text) + 1
    tail: list[int] = []
    used = 0
    for i in range(len(rendered) - 1, head[-1], -1):
        if tail and used + len(rendered[i]) + 1 > limit - cap:
            break
        tail.append(i)
        used += len(rendered[i]) + 1
    tail.reverse()
    if not tail or tail[0] == head[-1] + 1:
        return "\n".join(rendered), everything
    marker = f"[L{head[-1] + 2}-L{tail[0]} omitted]"
    shown = [rendered[i] for i in head] + [marker] + [rendered[i] for i in tail]
    return "\n".join(shown), frozenset(i + 1 for i in head + tail)


Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def _run_subprocess(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


class DockerExecutor:
    def __init__(
        self,
        data_dir,
        *,
        image: str = "dataclaw:0.1.0",
        name: str | None = None,
        runner: Runner | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.image = image
        self.name = name or f"tkboost-probe-{uuid.uuid4().hex[:8]}"
        self._run = runner or _run_subprocess

    def _check(self, args: list[str]) -> None:
        proc = self._run(args, 60)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(args[:3])} failed: {(proc.stdout or b'').decode(errors='replace')}")

    def start(self) -> None:
        self._check([
            "docker", "run", "-d", "--rm",
            "--name", self.name,
            "--network", "none",
            "-v", f"{self.data_dir}:{WORKSPACE}/database:ro",
            "-w", WORKSPACE,
            self.image,
            "tail", "-f", "/dev/null",
        ])
        self._check([
            "docker", "exec", self.name, "bash", "-c",
            f"rm -rf /root/.openclaw/workspace && ln -s {WORKSPACE} /root/.openclaw/workspace",
        ])

    def run(self, command: str, timeout: int) -> ExecResult:
        args = [
            "docker", "exec", "-w", WORKSPACE, self.name,
            "timeout", str(timeout), "bash", "-c", command,
        ]
        try:
            proc = self._run(args, timeout + HOST_TIMEOUT_GRACE)
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            return ExecResult(partial, None, True)
        output = (proc.stdout or b"").decode("utf-8", errors="replace")
        return ExecResult(output, proc.returncode, proc.returncode == TIMEOUT_EXIT_CODE)

    def stop(self) -> None:
        self._run(["docker", "rm", "-f", self.name], 60)


class ProbeSession:
    """Probes P1, P2, ...; the full output is kept, the LLM sees a line-numbered, truncated view."""

    def __init__(self, executor, *, timeout: int = 60, max_llm_chars: int = 4000):
        self.executor = executor
        self.timeout = timeout
        self.max_llm_chars = max_llm_chars
        self.records: list[ProbeRecord] = []

    def __enter__(self) -> "ProbeSession":
        self.executor.start()
        return self

    def __exit__(self, *exc) -> None:
        self.executor.stop()

    def run(self, command: str) -> ProbeRecord:
        if not command.strip():
            raise ValueError("probe command is empty")
        result = self.executor.run(command, self.timeout)
        record = ProbeRecord(len(self.records) + 1, command, *result)
        self.records.append(record)
        return record

    def get(self, number: int) -> ProbeRecord | None:
        return self.records[number - 1] if 1 <= number <= len(self.records) else None

    def lines(self, record: ProbeRecord) -> ProbeLines:
        _, visible = numbered_view(record.output, self.max_llm_chars)
        return ProbeLines(tuple(record.output.splitlines()), visible)

    def llm_view(self, record: ProbeRecord) -> str:
        status = f"timed out after {self.timeout}s" if record.timed_out else f"exit {record.exit_code}"
        body = numbered_view(record.output, self.max_llm_chars)[0] if record.output else "(no output)"
        return f"PROBE_RESULT P{record.number} ({status}):\n{body}"
