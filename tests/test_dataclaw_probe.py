"""Probe sessions: numbered shell probes whose full output is kept for the gates."""

import os
import subprocess

import pytest

from tkstore.dataclaw.probe import DockerExecutor, ExecResult, ProbeSession


class FakeExecutor:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands = []
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def run(self, command, timeout):
        self.commands.append((command, timeout))
        return self.outputs.pop(0)

    def stop(self):
        self.stopped = True


def test_probes_are_numbered_and_keep_full_output():
    executor = FakeExecutor([ExecResult("a" * 50, 0, False), ExecResult("second", 1, False)])
    with ProbeSession(executor, timeout=60, max_llm_chars=10) as session:
        first = session.run("head -3 ./database/internal_metrics.csv")
        second = session.run("grep x y")
    assert (first.number, second.number) == (1, 2)
    assert first.output == "a" * 50
    assert session.get(2).exit_code == 1
    assert session.get(3) is None
    assert executor.commands[0] == ("head -3 ./database/internal_metrics.csv", 60)


def test_llm_view_truncates_but_record_does_not():
    executor = FakeExecutor([ExecResult("H" * 10 + "M" * 80 + "T" * 10, 0, False)])
    with ProbeSession(executor, timeout=60, max_llm_chars=20) as session:
        record = session.run("cat big")
    view = session.llm_view(record)
    assert view.startswith("PROBE_RESULT #1 (exit 0)")
    assert "H" * 10 in view and "T" * 10 in view and "80 chars omitted" in view
    assert record.output.count("M") == 80


def test_timeout_is_reported():
    executor = FakeExecutor([ExecResult("partial", 124, True)])
    with ProbeSession(executor, timeout=5, max_llm_chars=100) as session:
        record = session.run("sleep 100")
    assert record.timed_out
    assert "timed out after 5s" in session.llm_view(record)


def test_empty_output_is_marked():
    with ProbeSession(FakeExecutor([ExecResult("", 0, False)]), timeout=5) as session:
        assert "(no output)" in session.llm_view(session.run("true"))


def test_session_starts_and_always_stops_the_executor():
    executor = FakeExecutor([])
    with pytest.raises(RuntimeError):
        with ProbeSession(executor, timeout=5):
            assert executor.started
            raise RuntimeError("boom")
    assert executor.stopped


def test_blank_command_is_rejected_without_running():
    executor = FakeExecutor([])
    with ProbeSession(executor, timeout=5) as session:
        with pytest.raises(ValueError):
            session.run("   ")
    assert executor.commands == []


def test_docker_executor_isolates_the_container(tmp_path):
    calls = []

    def runner(args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"out", stderr=None)

    executor = DockerExecutor(tmp_path, image="img:1", name="probe-x", runner=runner)
    executor.start()
    result = executor.run("head -1 ./database/a.csv", timeout=60)
    executor.stop()

    start = calls[0]
    assert start[:3] == ["docker", "run", "-d"]
    assert "--network" in start and start[start.index("--network") + 1] == "none"
    assert f"{tmp_path}:/tmp_workspace/database:ro" in start
    assert start[start.index("-w") + 1] == "/tmp_workspace"
    assert "img:1" in start
    assert "ln -s /tmp_workspace /root/.openclaw/workspace" in " ".join(calls[1])
    assert calls[2] == [
        "docker", "exec", "-w", "/tmp_workspace", "probe-x",
        "timeout", "60", "bash", "-c", "head -1 ./database/a.csv",
    ]
    assert result == ExecResult("out", 0, False)
    assert calls[-1] == ["docker", "rm", "-f", "probe-x"]


def test_docker_executor_maps_exit_124_to_timeout(tmp_path):
    def runner(args, timeout):
        code = 124 if args[:2] == ["docker", "exec"] and "sleep 9" in args else 0
        return subprocess.CompletedProcess(args, code, stdout=b"", stderr=None)

    executor = DockerExecutor(tmp_path, name="probe-y", runner=runner)
    assert executor.run("sleep 9", timeout=1).timed_out


def test_docker_executor_decodes_invalid_utf8(tmp_path):
    def runner(args, timeout):
        return subprocess.CompletedProcess(args, 0, stdout="广东".encode() + b"\xff", stderr=None)

    assert DockerExecutor(tmp_path, runner=runner).run("x", 5).output.startswith("广东")


@pytest.mark.skipif(
    os.environ.get("DATACLAW_DOCKER_TESTS") != "1",
    reason="needs docker and the dataclaw:0.1.0 image; set DATACLAW_DOCKER_TESTS=1",
)
def test_real_container_replays_agent_style_commands():
    data_dir = os.path.expanduser("~/DataClaw/assets/database")
    with ProbeSession(DockerExecutor(data_dir), timeout=20) as session:
        header = session.run("cd ./database && head -1 internal_metrics.csv")
        via_link = session.run("ls /root/.openclaw/workspace/database/enterprise | wc -l")
        write = session.run("touch ./database/x")
        net = session.run("python3 -c 'import urllib.request; urllib.request.urlopen(\"http://example.com\", timeout=3)'")
        slow = session.run("sleep 30")
    assert header.exit_code == 0 and header.output.strip()
    assert via_link.output.strip() == "9"
    assert write.exit_code != 0 and "Read-only" in write.output
    assert net.exit_code != 0
    assert slow.timed_out
