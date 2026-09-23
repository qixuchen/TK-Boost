"""The context-aware FilterKnowledge, and how it reaches retrieval.

Stage 9, second half. The prompt's contents are covered in `test_context_filter_prompt.py`;
here the concern is the call itself (chunking, parsing, what happens when the model fails)
and the plumbing that decides whether this implementation runs at all.

The flag stays off by default, and when it is off retrieval calls upstream's
`_llm_filter_relevant_rules` unchanged. That is what makes "round H is reproducible" a
structural property rather than a matter of test discipline: with the flag off, the code
that runs is the same code that ran then, in a file this stage does not touch.

One guard test matters more than it looks: switching this on must not alter what the
*refiner* is given. Stage 9 changes which rules are selected, and nothing else. The
downstream SQL text is needed here to build the prompt, but the refiner only receives it
when `--validate-fix-in-context` asks for it, which is a different experiment.
"""

import csv
import json

import pytest

from src.agents import sql_agent_runner as runner


def _args(argv: list):
    return runner._build_parser().parse_args(argv)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "store.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mem_id", "instance_id", "db", "scope", "sql_operations",
                    "table", "column", "data_type", "nulls", "rule"])
        for n in range(1, 41):
            w.writerow([str(n), f"local{n}", "otherdb", "generic", "select",
                        "all", "all", "all", "all", f"rule text {n}"])
    return str(path)


def _cands(n):
    return [{"mem_id": str(i), "scope": "generic", "sql_operations": ["select"],
             "table": "all", "column": "all", "data_type": "all", "nulls": "all",
             "rule": f"rule text {i}"} for i in range(1, n + 1)]


def _context(**over):
    kwargs = dict(user_question="q", probe_exchanges=[], previous_ctes="",
                  downstream="", full_query=False)
    kwargs.update(over)
    return runner.FragmentContext(**kwargs)


class _Recorder:
    """Stands in for litellm. Returns `selected` for each call, recording every prompt."""

    def __init__(self, selected, content=None):
        self.selected = selected
        self.content = content
        self.prompts = []

    def __call__(self, model, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        body = self.content if self.content is not None else json.dumps(
            {"selected_indices": self.selected, "reasoning": "kept the plausible ones"})
        return {"choices": [{"message": {"content": body}}]}


class TestSelectingAndParsing:
    @pytest.fixture
    def call(self, monkeypatch):
        def run(candidates, recorder, **over):
            monkeypatch.setattr(runner.litellm, "completion", recorder)
            return runner._llm_filter_with_context(
                candidates, tkstore_path=over.pop("tkstore_path"), db="thisdb",
                context=_context(**over), model="gpt-4.1", current_sql="SELECT 1")
        return run

    def test_only_the_chosen_rules_come_back(self, store, call):
        recorder = _Recorder([1, 3])

        result = call(_cands(4), recorder, tkstore_path=store)

        assert [r["mem_id"] for r in result.selected] == ["1", "3"]

    def test_the_models_reasoning_is_kept(self, store, call):
        """Upstream parses `reasoning` out of the response and then never reads it, so
        today there is no way to ask why a harmful rule was selected."""
        result = call(_cands(3), _Recorder([1]), tkstore_path=store)

        assert result.reasoning == ["kept the plausible ones"]

    def test_indices_outside_the_list_are_ignored(self, store, call):
        result = call(_cands(3), _Recorder([1, 99, 0, -2]), tkstore_path=store)

        assert [r["mem_id"] for r in result.selected] == ["1"]

    def test_the_returned_rules_carry_their_source_database(self, store, call):
        """So the caller's report can say where a selected rule came from."""
        result = call(_cands(2), _Recorder([1]), tkstore_path=store)

        assert result.selected[0]["db"] == "otherdb"

    def test_a_response_that_is_not_json_keeps_every_candidate(self, store, call):
        """Fail open, as upstream does: a filter that cannot answer must not silently
        shrink the rule set."""
        result = call(_cands(3), _Recorder(None, content="I could not decide."),
                      tkstore_path=store)

        assert [r["mem_id"] for r in result.selected] == ["1", "2", "3"]

    def test_a_raising_call_keeps_every_candidate(self, store, monkeypatch):
        def explode(model, messages, **kwargs):
            raise RuntimeError("rate limited")
        monkeypatch.setattr(runner.litellm, "completion", explode)

        result = runner._llm_filter_with_context(
            _cands(3), tkstore_path=store, db="thisdb", context=_context(),
            model="gpt-4.1", current_sql="SELECT 1")

        assert len(result.selected) == 3

    def test_a_failure_is_recorded_rather_than_hidden(self, store, monkeypatch):
        monkeypatch.setattr(runner.litellm, "completion",
                            lambda model, messages, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        result = runner._llm_filter_with_context(
            _cands(2), tkstore_path=store, db="thisdb", context=_context(),
            model="gpt-4.1", current_sql="SELECT 1")

        assert any("boom" in note for note in result.reasoning)

    def test_no_candidates_means_no_call(self, store, monkeypatch):
        monkeypatch.setattr(runner.litellm, "completion",
                            lambda *a, **k: pytest.fail("must not call the model"))

        result = runner._llm_filter_with_context(
            [], tkstore_path=store, db="thisdb", context=_context(),
            model="gpt-4.1", current_sql="SELECT 1")

        assert result.selected == []


class TestChunking:
    """Kept at 15 per call, as upstream does, so that fragments with many candidates do not
    also change how the list is presented. Measured on round H: 198 of 354 fragments carry
    more than 15 candidates and 156 carry 15 or fewer, so both branches are live -- which is
    exactly why upstream's two separate prompts were a hazard."""

    @pytest.fixture
    def call(self, monkeypatch):
        def run(n, recorder, tkstore_path):
            monkeypatch.setattr(runner.litellm, "completion", recorder)
            return runner._llm_filter_with_context(
                _cands(n), tkstore_path=tkstore_path, db="thisdb", context=_context(),
                model="gpt-4.1", current_sql="SELECT 1")
        return run

    def test_sixteen_candidates_take_two_calls(self, store, call):
        recorder = _Recorder([1])

        call(16, recorder, store)

        assert len(recorder.prompts) == 2

    def test_fifteen_candidates_take_one_call(self, store, call):
        recorder = _Recorder([1])

        call(15, recorder, store)

        assert len(recorder.prompts) == 1

    def test_indices_are_local_to_each_chunk(self, store, call):
        """Every chunk numbers its rules from 1, so index 1 in the second chunk is the
        sixteenth candidate."""
        recorder = _Recorder([1])

        result = call(16, recorder, store)

        assert [r["mem_id"] for r in result.selected] == ["1", "16"]

    def test_every_chunk_carries_the_full_context(self, store, monkeypatch):
        """The context is the point of this stage; dropping it from later chunks would
        leave most fragments filtered the old way."""
        recorder = _Recorder([1])
        monkeypatch.setattr(runner.litellm, "completion", recorder)

        runner._llm_filter_with_context(
            _cands(16), tkstore_path=store, db="thisdb",
            context=_context(user_question="count the sessions"),
            model="gpt-4.1", current_sql="SELECT 1")

        assert all("count the sessions" in p for p in recorder.prompts)

    def test_a_failing_chunk_keeps_its_own_candidates_only(self, store, monkeypatch):
        calls = {"n": 0}

        def flaky(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second chunk failed")
            return {"choices": [{"message": {"content": json.dumps(
                {"selected_indices": [1], "reasoning": "ok"})}}]}
        monkeypatch.setattr(runner.litellm, "completion", flaky)

        result = runner._llm_filter_with_context(
            _cands(17), tkstore_path=store, db="thisdb", context=_context(),
            model="gpt-4.1", current_sql="SELECT 1")

        assert [r["mem_id"] for r in result.selected] == ["1", "16", "17"]


class TestRetrievalDispatch:
    @pytest.fixture
    def upstream(self, monkeypatch):
        seen = {}

        def fake(sql_text, candidates, db=None, model=None):
            seen["called"] = True
            return candidates
        monkeypatch.setattr(runner, "_llm_filter_relevant_rules", fake)
        monkeypatch.setattr(runner, "search_index_for_sql", lambda *a, **k: _cands(3))
        return seen

    def test_without_a_context_the_upstream_filter_runs(self, store, upstream):
        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="thisdb",
            use_llm_filtering=True, filter_model="gpt-4.1")

        assert upstream.get("called") is True

    def test_with_a_context_the_upstream_filter_is_bypassed(self, store, upstream, monkeypatch):
        monkeypatch.setattr(runner, "_llm_filter_with_context",
                            lambda c, **k: runner._ContextFilterResult(list(c[:1]), []))

        runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="thisdb",
            use_llm_filtering=True, filter_model="gpt-4.1", context=_context())

        assert upstream.get("called") is None

    def test_the_selection_is_returned(self, store, upstream, monkeypatch):
        monkeypatch.setattr(runner, "_llm_filter_with_context",
                            lambda c, **k: runner._ContextFilterResult(list(c[:2]), ["why"]))

        candidates, selected = runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="thisdb",
            use_llm_filtering=True, filter_model="gpt-4.1", context=_context())

        assert len(candidates) == 3
        assert [r["mem_id"] for r in selected] == ["1", "2"]

    def test_turning_off_llm_filtering_skips_it_entirely(self, store, upstream, monkeypatch):
        monkeypatch.setattr(runner, "_llm_filter_with_context",
                            lambda *a, **k: pytest.fail("must not run"))

        candidates, selected = runner._retrieve_rules_for(
            sql_text="SELECT 1", tkstore_path=store, db="thisdb",
            use_llm_filtering=False, filter_model="gpt-4.1", context=_context())

        assert selected == candidates


class TestCli:
    def test_the_flag_defaults_to_off(self):
        assert _args(["--instance-id", "local001"]).context_filter is False

    def test_it_is_carried_in_the_knowledge_options(self, store):
        options = runner._knowledge_options(_args([
            "--instance-id", "local001", "--refine-cte", "--tkstore", store,
            "--context-filter",
        ]))

        assert options["context_filter"] is True

    def test_it_requires_a_store(self):
        with pytest.raises(ValueError, match="context-filter"):
            runner._knowledge_options(
                _args(["--instance-id", "local001", "--refine-cte", "--context-filter"]))


TWO_CTE_SQL = """WITH totals AS (
SELECT 1 AS n FROM t
),
shares AS (
SELECT n FROM totals
)
SELECT * FROM shares"""


class _Executor:
    def execute(self, sql):
        return ["n"], [(1,)]

    def close(self):
        pass


class TestRunnerWiring:
    @pytest.fixture
    def refiner_calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(runner, "refiner_run",
                            lambda **kw: calls.append(kw) or {"status": "ok", "issues": []})
        monkeypatch.setattr(runner, "make_executor", lambda *_a, **_k: _Executor())
        monkeypatch.setattr(runner, "write_csv", lambda *_a, **_k: None)
        return calls

    @pytest.fixture
    def retrievals(self, monkeypatch):
        seen = []

        def fake(**kwargs):
            seen.append(kwargs)
            return [], []
        monkeypatch.setattr(runner, "_retrieve_rules_for", fake)
        return seen

    def _refine(self, tmp_path, store, **kw):
        return runner.perform_refinement_and_revision(
            inst=runner.Instance(instance_id="local001", db="testdb",
                                 question="how many sessions"),
            final_sql=TWO_CTE_SQL, predicted_cte_hint=None, engine="sqlite",
            db_path_or_cred=str(tmp_path / "x.sqlite"), messages=[], out_dir=tmp_path,
            model="gpt-4.1", verbose=False, tkstore_path=store, **kw)

    def test_no_context_is_built_when_the_flag_is_off(self, tmp_path, store,
                                                     refiner_calls, retrievals):
        self._refine(tmp_path, store)

        assert all(call.get("context") is None for call in retrievals)

    def test_the_question_reaches_the_filter(self, tmp_path, store, refiner_calls, retrievals):
        self._refine(tmp_path, store, context_filter=True)

        assert retrievals[0]["context"].user_question == "how many sessions"

    def test_the_agents_probes_reach_the_filter(self, tmp_path, store,
                                               refiner_calls, retrievals):
        (tmp_path / "messages.json").write_text(json.dumps([
            {"role": "assistant", "content": "<sql>PRAGMA table_info(t)</sql>"},
            {"role": "user", "content": "SQL_RESULT_TABLE:\nn"},
        ]), encoding="utf-8")

        self._refine(tmp_path, store, context_filter=True)

        assert retrievals[0]["context"].probe_exchanges[0][0] == "PRAGMA table_info(t)"

    def test_the_first_cte_is_told_what_reads_it(self, tmp_path, store,
                                                refiner_calls, retrievals):
        self._refine(tmp_path, store, context_filter=True)

        assert "shares" in retrievals[0]["context"].downstream

    def test_the_final_select_stage_is_marked_as_the_full_query(self, tmp_path, store,
                                                               refiner_calls, retrievals):
        self._refine(tmp_path, store, context_filter=True)

        assert retrievals[-1]["context"].full_query is True
        assert retrievals[-1]["context"].downstream == ""

    def test_the_refiner_is_not_given_the_downstream_as_a_side_effect(
            self, tmp_path, store, refiner_calls, retrievals):
        """Showing the refiner its downstream is stage 6's experiment, gated behind
        --validate-fix-in-context. Stage 9 needs the same text for its prompt but must not
        hand it to the refiner, or the two experiments become one."""
        self._refine(tmp_path, store, context_filter=True)

        assert refiner_calls[0].get("downstream") in (None, "")

    def test_the_report_records_the_flag(self, tmp_path, store, refiner_calls, retrievals):
        self._refine(tmp_path, store, context_filter=True)

        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        assert report["context_filter"] is True

    def test_the_report_records_what_was_excluded(self, tmp_path, store,
                                                 refiner_calls, monkeypatch):
        """Round H could not answer "why was rule 26 selected for local330" from artifacts."""
        monkeypatch.setattr(runner, "_retrieve_rules_for", lambda **_k: (
            [{"mem_id": "26"}, {"mem_id": "42"}], [{"mem_id": "26"}]))

        self._refine(tmp_path, store, context_filter=True)

        report = json.loads((tmp_path / "retrieved_rules.json").read_text())
        first = report["retrievals"][0]
        assert first["selected"] == ["26"]
        assert first["excluded"] == ["42"]
