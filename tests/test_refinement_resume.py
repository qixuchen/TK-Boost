"""Resuming an interrupted refinement pass.

A refinement arm over 86 instances takes about 14 hours. Restarting the whole
directory on every interruption is not affordable, so a finished instance has to be
skippable. `--split` cannot help here: `main` dispatches to
`run_refinement_on_existing_outputs` before instance selection runs, and that
function walks the directory instead.
"""

import io
import json

import pytest

from src.agents import sql_agent_runner as runner


MARKER = "refinement_complete.marker"


def _instance_dir(parent, name, sql="SELECT 1"):
    d = parent / name
    d.mkdir(parents=True)
    (d / "execution_query.sql").write_text(sql, encoding="utf-8")
    return d


def _partial_instance_dir(parent, name):
    """What an instance interrupted mid-agent-loop leaves behind: the directory is
    created up front, but `execution_query.sql` is only written once the loop returns."""
    d = parent / name
    d.mkdir(parents=True)
    (d / "messages.json").write_text("[]", encoding="utf-8")
    return d


class TestHasAgentOutput:
    """The one predicate for 'this instance finished its agent loop'."""

    def test_non_empty_sql_counts(self, tmp_path):
        assert runner._has_agent_output(_instance_dir(tmp_path, "local001_20260101_000000")) is True

    def test_a_missing_file_does_not_count(self, tmp_path):
        assert runner._has_agent_output(_partial_instance_dir(tmp_path, "local001_20260101_000000")) is False

    def test_an_empty_file_does_not_count(self, tmp_path):
        """The write is `final_sql or ""`, so a failed run can leave the file empty."""
        d = _instance_dir(tmp_path, "local001_20260101_000000", sql="")

        assert runner._has_agent_output(d) is False


class TestDiscardIncompleteInstanceDirs:
    """Interrupting the agent step leaves a directory with no `execution_query.sql`.
    Rerunning creates a second, timestamped directory for the same instance, and the
    leftover then travels into both arms where it is counted as a refinement failure
    forever. Clearing it is the runner's job, not the operator's."""

    def test_a_partial_directory_is_discarded(self, tmp_path):
        _partial_instance_dir(tmp_path, "local001_20260101_000000")

        discarded = runner._discard_incomplete_instance_dirs(tmp_path)

        assert discarded == ["local001_20260101_000000"]
        assert not (tmp_path / "local001_20260101_000000").exists()

    def test_a_finished_directory_is_kept_whole(self, tmp_path):
        d = _instance_dir(tmp_path, "local001_20260101_000000")
        (d / "messages.json").write_text("[]", encoding="utf-8")

        assert runner._discard_incomplete_instance_dirs(tmp_path) == []
        assert (d / "execution_query.sql").exists()
        assert (d / "messages.json").exists()

    def test_an_empty_query_file_is_discarded(self, tmp_path):
        _instance_dir(tmp_path, "local001_20260101_000000", sql="")

        assert runner._discard_incomplete_instance_dirs(tmp_path) == ["local001_20260101_000000"]

    def test_hidden_directories_are_left_alone(self, tmp_path):
        (tmp_path / ".ipynb_checkpoints").mkdir()

        assert runner._discard_incomplete_instance_dirs(tmp_path) == []
        assert (tmp_path / ".ipynb_checkpoints").exists()

    def test_an_absent_base_is_not_an_error(self, tmp_path):
        assert runner._discard_incomplete_instance_dirs(tmp_path / "nope") == []


class TestSyncInstanceDirs:
    """Copying the tree in one shot is what makes the pass all-or-nothing."""

    def test_fresh_destination_receives_every_instance(self, tmp_path):
        src = tmp_path / "src"
        _instance_dir(src, "local001_20260101_000000")
        _instance_dir(src, "local002_20260101_000000")

        synced = runner._sync_instance_dirs(src, tmp_path / "dest")

        assert [d.name for d in synced] == [
            "local001_20260101_000000",
            "local002_20260101_000000",
        ]
        assert (tmp_path / "dest/local001_20260101_000000/execution_query.sql").read_text() == "SELECT 1"

    def test_work_already_in_the_destination_is_kept(self, tmp_path):
        """Re-copying would discard the refinement artifacts of a finished instance."""
        src = tmp_path / "src"
        _instance_dir(src, "local001_20260101_000000", sql="SELECT 1")
        dest = tmp_path / "dest"
        _instance_dir(dest, "local001_20260101_000000", sql="SELECT 2 -- revised")
        (dest / "local001_20260101_000000" / "execution_query_after_x.sql").write_text("x", encoding="utf-8")

        runner._sync_instance_dirs(src, dest)

        kept = dest / "local001_20260101_000000"
        assert kept.joinpath("execution_query.sql").read_text() == "SELECT 2 -- revised"
        assert kept.joinpath("execution_query_after_x.sql").exists()

    def test_missing_instances_are_added_alongside_existing_ones(self, tmp_path):
        src = tmp_path / "src"
        _instance_dir(src, "local001_20260101_000000")
        _instance_dir(src, "local002_20260101_000000")
        dest = tmp_path / "dest"
        _instance_dir(dest, "local001_20260101_000000")

        synced = runner._sync_instance_dirs(src, dest)

        assert len(synced) == 2
        assert (dest / "local002_20260101_000000/execution_query.sql").exists()

    def test_inherited_instance_marker_is_dropped(self, tmp_path):
        """A source that was itself refined carries markers. Trusting them would skip
        every instance and produce an arm that is a plain copy."""
        src = tmp_path / "src"
        d = _instance_dir(src, "local001_20260101_000000")
        (d / MARKER).write_text("refined by an earlier pass\n", encoding="utf-8")

        runner._sync_instance_dirs(src, tmp_path / "dest")

        assert not (tmp_path / "dest/local001_20260101_000000" / MARKER).exists()

    def test_inherited_directory_marker_is_dropped(self, tmp_path):
        """The directory-level marker would make the next run report 'already complete'."""
        src = tmp_path / "src"
        _instance_dir(src, "local001_20260101_000000")
        (src / MARKER).write_text("done\n", encoding="utf-8")

        runner._sync_instance_dirs(src, tmp_path / "dest")

        assert not (tmp_path / "dest" / MARKER).exists()

    def test_marker_written_by_this_destination_survives(self, tmp_path):
        src = tmp_path / "src"
        _instance_dir(src, "local001_20260101_000000")
        dest = tmp_path / "dest"
        d = _instance_dir(dest, "local001_20260101_000000")
        (d / MARKER).write_text("done\n", encoding="utf-8")

        runner._sync_instance_dirs(src, dest)

        assert (dest / "local001_20260101_000000" / MARKER).exists()

    def test_hidden_directories_are_not_instances(self, tmp_path):
        src = tmp_path / "src"
        _instance_dir(src, "local001_20260101_000000")
        (src / ".ipynb_checkpoints").mkdir()

        synced = runner._sync_instance_dirs(src, tmp_path / "dest")

        assert [d.name for d in synced] == ["local001_20260101_000000"]

    def test_an_instance_with_no_agent_output_is_not_synced(self, tmp_path):
        """The agent step can leave one behind by failing on a single instance, and that
        happens without stopping the pipeline. Refining it is impossible, so counting it
        as a failure would make `Failed: n/86` non-zero for good and withhold the
        directory receipt no matter how often the arm is rerun."""
        src = tmp_path / "src"
        _instance_dir(src, "local001_20260101_000000")
        _partial_instance_dir(src, "local002_20260101_000000")

        synced = runner._sync_instance_dirs(src, tmp_path / "dest")

        assert [d.name for d in synced] == ["local001_20260101_000000"]
        assert not (tmp_path / "dest/local002_20260101_000000").exists()


class TestInstanceMarker:
    def test_a_fresh_directory_is_not_refined(self, tmp_path):
        assert runner._instance_is_refined(tmp_path) is False

    def test_marking_makes_it_refined(self, tmp_path):
        runner._mark_instance_refined(tmp_path)

        assert runner._instance_is_refined(tmp_path) is True
        assert (tmp_path / MARKER).is_file()


@pytest.fixture
def refine_env(tmp_path, monkeypatch):
    """Stub everything the per-instance body needs except the resume decision."""
    monkeypatch.setattr("sys.stdin", io.StringIO())
    monkeypatch.setattr(
        runner,
        "load_instances_from_jsonl",
        lambda _p: [
            runner.Instance(instance_id=f"local00{i}", db="testdb", question="q")
            for i in (1, 2, 3, 4)
        ],
    )
    monkeypatch.setattr(runner, "resolve_sqlite_db_path", lambda *_a, **_k: str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(runner, "get_system_prompt", lambda *_a, **_k: "sys")
    monkeypatch.setattr(runner, "build_user_message", lambda *_a, **_k: "user")
    monkeypatch.setattr(runner, "load_external_knowledge", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_choose_and_mark_final_artifacts", lambda *_a, **_k: None)

    refined = []

    def fake_refine(**kwargs):
        refined.append(kwargs["inst"].instance_id)
        return "SELECT 1", None

    monkeypatch.setattr(runner, "perform_refinement_and_revision", fake_refine)
    return refined


def _run_refinement(src, dest, extra=()):
    args = runner._build_parser().parse_args(
        ["--refine-output", str(src), "--refine-output-dir", str(dest), *extra]
    )
    runner.run_refinement_on_existing_outputs(args)


class TestResume:
    @pytest.fixture
    def src(self, tmp_path):
        src = tmp_path / "agent_out"
        for i in (1, 2, 3):
            _instance_dir(src, f"local00{i}_20260101_000000")
        return src

    def test_a_first_pass_refines_everything(self, tmp_path, src, refine_env):
        _run_refinement(src, tmp_path / "arm")

        assert sorted(refine_env) == ["local001", "local002", "local003"]

    def test_each_refined_instance_gets_a_marker(self, tmp_path, src, refine_env):
        _run_refinement(src, tmp_path / "arm")

        for i in (1, 2, 3):
            assert (tmp_path / "arm" / f"local00{i}_20260101_000000" / MARKER).is_file()

    def test_the_instance_markers_alone_are_enough_to_skip(self, tmp_path, src, refine_env):
        """Without the directory-level short circuit the per-instance state must still hold."""
        dest = tmp_path / "arm"
        _run_refinement(src, dest)
        (dest / MARKER).unlink()
        refine_env.clear()

        _run_refinement(src, dest)

        assert refine_env == []

    def test_an_interrupted_pass_resumes_at_the_unfinished_instance(
        self, tmp_path, src, monkeypatch, refine_env
    ):
        """The point of the whole exercise: 2 of 3 done means 1 left to do.

        Ctrl-C is a `BaseException`, so it escapes the per-instance `except Exception`
        and the pass dies before writing the directory-level marker.
        """
        def interrupt_on_the_third(**kwargs):
            refine_env.append(kwargs["inst"].instance_id)
            if kwargs["inst"].instance_id == "local003":
                raise KeyboardInterrupt
            return "SELECT 1", None

        monkeypatch.setattr(runner, "perform_refinement_and_revision", interrupt_on_the_third)
        dest = tmp_path / "arm"
        with pytest.raises(KeyboardInterrupt):
            _run_refinement(src, dest)
        assert not (dest / MARKER).exists()
        refine_env.clear()
        monkeypatch.setattr(
            runner,
            "perform_refinement_and_revision",
            lambda **kw: (refine_env.append(kw["inst"].instance_id), ("SELECT 1", None))[1],
        )

        _run_refinement(src, dest)

        assert refine_env == ["local003"]

    def test_a_failed_instance_is_retried(self, tmp_path, src, monkeypatch, refine_env):
        """An instance that raised must not look finished."""
        def boom(**kwargs):
            refine_env.append(kwargs["inst"].instance_id)
            if kwargs["inst"].instance_id == "local002":
                raise RuntimeError("refiner blew up")
            return "SELECT 1", None

        monkeypatch.setattr(runner, "perform_refinement_and_revision", boom)
        dest = tmp_path / "arm"
        _run_refinement(src, dest)
        assert not (dest / "local002_20260101_000000" / MARKER).exists()
        refine_env.clear()
        monkeypatch.setattr(
            runner,
            "perform_refinement_and_revision",
            lambda **kw: (refine_env.append(kw["inst"].instance_id), ("SELECT 1", None))[1],
        )

        _run_refinement(src, dest)

        assert refine_env == ["local002"]

    def test_the_directory_marker_waits_for_every_instance(self, tmp_path, src, monkeypatch, refine_env):
        """Writing it while an instance failed would block the retry above."""
        def boom(**kwargs):
            if kwargs["inst"].instance_id == "local002":
                raise RuntimeError("refiner blew up")
            return "SELECT 1", None

        monkeypatch.setattr(runner, "perform_refinement_and_revision", boom)
        dest = tmp_path / "arm"

        _run_refinement(src, dest)

        assert not (dest / MARKER).exists()

    def test_an_existing_directory_is_never_wiped(self, tmp_path, src, monkeypatch, refine_env):
        """A stray `y` at the old `Overwrite? (y/n)` prompt destroyed the whole arm."""
        dest = tmp_path / "arm"
        _run_refinement(src, dest)
        (dest / MARKER).unlink()
        (dest / "local001_20260101_000000" / "keep_me.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("must not prompt"))

        _run_refinement(src, dest)

        assert (dest / "local001_20260101_000000" / "keep_me.txt").exists()

    def test_a_finished_pass_reruns_as_a_no_op(self, tmp_path, src, refine_env):
        """Rerunning a completed arm must not redo 14 hours of work."""
        dest = tmp_path / "arm"
        _run_refinement(src, dest)
        assert (dest / MARKER).exists()
        refine_env.clear()

        _run_refinement(src, dest)

        assert refine_env == []

    def test_instances_added_after_a_complete_pass_are_still_refined(self, tmp_path, src, refine_env):
        """The directory marker records the instances of *one* pass, but the source can
        grow afterwards -- rehearsing on 3 instances and then scaling the same prefix to
        86 must not turn the arm into a no-op for the other 83. Silently unrefined
        instances score `score_final=0`, which reads as a refiner failure rather than as
        work that never ran."""
        dest = tmp_path / "arm"
        _run_refinement(src, dest)
        refine_env.clear()
        _instance_dir(src, "local004_20260101_000000")

        _run_refinement(src, dest)

        assert refine_env == ["local004"]

    def test_a_stale_directory_marker_does_not_block_an_untouched_destination(self, tmp_path, src, refine_env):
        """A marker with no instance directories beside it cannot mean the work is done."""
        dest = tmp_path / "arm"
        dest.mkdir()
        (dest / MARKER).write_text("done\n", encoding="utf-8")

        _run_refinement(src, dest)

        assert sorted(refine_env) == ["local001", "local002", "local003"]


class TestHasCompletedOutput:
    """The generation path writes `execution_query.sql` before refinement starts, so on
    its own that file cannot mean 'this instance is finished' when refinement is on."""

    def _out_base(self, tmp_path, sql="SELECT 1"):
        _instance_dir(tmp_path, "local001_20260101_000000", sql=sql)
        return tmp_path

    def test_generated_sql_counts_as_complete(self, tmp_path):
        assert runner._has_completed_output("local001", self._out_base(tmp_path)) is True

    def test_empty_sql_does_not_count(self, tmp_path):
        assert runner._has_completed_output("local001", self._out_base(tmp_path, sql="")) is False

    def test_absent_instance_does_not_count(self, tmp_path):
        assert runner._has_completed_output("local999", self._out_base(tmp_path)) is False

    def test_refinement_requires_its_own_marker(self, tmp_path):
        out_base = self._out_base(tmp_path)

        assert runner._has_completed_output("local001", out_base, require_refinement=True) is False

        runner._mark_instance_refined(out_base / "local001_20260101_000000")
        assert runner._has_completed_output("local001", out_base, require_refinement=True) is True
