"""Building blocks for a FilterKnowledge that sees the whole task.

Stage 9, first half: the pure functions that assemble what the filter is shown. The filter
call itself and the runner wiring are in `test_context_filter.py`.

Why this exists at all: today FilterKnowledge is given the current CTE and a list of
candidate rules, and nothing else. It therefore answers "is there a surface resemblance
between this rule's wording and this fragment of SQL" rather than "would this rule help
judge this CTE without pushing the refiner to break the rest of the query". In round H that
cost `local330` its correctness -- rule 26, mined from `California_Traffic_Collision`, was
selected for a path-normalising CTE in the `log` database and led the refiner to add
SELECT DISTINCT to an event log, which changes the event counts the question asks for. The
main agent had already recorded, in its own probing history, both that the bare '/' case
was deliberate and that deduplication happens downstream over (session, path).

Two long-standing defects in the handover between the two filtering steps are fixed here:

  * `search_index_for_sql` omits each rule's `db` from the dictionaries it returns, so
    `_llm_filter_relevant_rules` renders every rule as `db=all` and its "database-specific
    rules first" bucketing never fires. Measured on local330: all 6 candidates rendered
    `db=all`.
  * the prompt exists in two copies, one per candidate-count branch, and measured on round
    H's 354 fragments the two branches carry 56% and 44% of the traffic.

Both disappear here because this is one function with one prompt, and the source database
is filled in from the store by `_store_rule_index`.
"""

import csv
import json

import pytest

from src.agents import sql_agent_runner as runner


# --- Store fixture -----------------------------------------------------------------

STORE_ROWS = [
    # mem_id, db, scope, table, column
    ("26", "California_Traffic_Collision", "generic", "all", "all"),
    ("42", "SQLITE_SAKILA", "generic", "all", "all"),
    ("58", "bank_sales_trading", "db", "transactions", "txn_type"),
    ("60", "bank_sales_trading", "db", "all", "all"),
    ("70", "bank_sales_trading", "generic", "all", "all"),
]


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "store.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mem_id", "instance_id", "db", "scope", "sql_operations",
                    "table", "column", "data_type", "nulls", "rule"])
        for mem_id, db, scope, table, column in STORE_ROWS:
            w.writerow([mem_id, f"local{mem_id}", db, scope, "select;case",
                        table, column, "all", "all", f"rule text {mem_id}"])
    return str(path)


def _cand(mem_id, scope="generic", **extra):
    """A candidate shaped like `search_index_for_sql` returns it: note there is no `db`."""
    base = {
        "mem_id": mem_id,
        "scope": scope,
        "sql_operations": ["select", "case"],
        "table": "all",
        "column": "all",
        "data_type": "all",
        "nulls": "all",
        "rule": f"rule text {mem_id}",
    }
    base.update(extra)
    return base


# --- Probe evidence ----------------------------------------------------------------

AGENT_MESSAGES = [
    {"role": "system", "content": "You are a careful SQL agent working with SQLite."},
    {"role": "user", "content": "[USER_QUESTION] count unique sessions"},
    {"role": "assistant", "content": "<think>First I will list the tables.</think>"},
    {"role": "assistant", "content": "<sql> SELECT name FROM sqlite_master; </sql>"},
    {"role": "user", "content": "SQL_RESULT_TABLE:\nname\n----\nactivity_log"},
    {"role": "assistant", "content": "There is an activity_log table."},
    {"role": "user", "content": "Send one <sql> now."},
    {"role": "assistant", "content": "<sql> SELECT DISTINCT path FROM activity_log; </sql>"},
    {"role": "user", "content": "SQL_RESULT_TABLE:\npath\n----\n/detail/\n/detail"},
    {"role": "assistant", "content": "<sql> SELECT bogus FROM activity_log; </sql>"},
    {"role": "user", "content": "SQL_ERROR: no such column: bogus"},
    {"role": "assistant", "content": "<solution>\nSELECT 1\n</solution>"},
]


class TestProbeExchanges:
    """The agent's probing history, paired up. Shared with stage 10, which replays the same
    pairs as refiner conversation turns instead of rendering them into a prompt."""

    def test_each_probe_is_paired_with_its_result(self):
        pairs = runner._probe_exchanges(AGENT_MESSAGES)

        assert len(pairs) == 3
        assert pairs[0][0] == "SELECT name FROM sqlite_master;"
        assert "activity_log" in pairs[0][1]

    def test_a_failed_probe_keeps_its_error(self):
        """A probe that failed is evidence too: it says what the database does not have."""
        pairs = runner._probe_exchanges(AGENT_MESSAGES)

        assert pairs[2][0] == "SELECT bogus FROM activity_log;"
        assert pairs[2][1].startswith("SQL_ERROR:")

    def test_thinking_and_commentary_are_left_out(self):
        """They are the agent's reasoning stream, not checkable evidence, and they are the
        bulk of messages.json -- local330 has 28 messages of which only 12 are probe pairs."""
        rendered = " ".join(sql + res for sql, res in runner._probe_exchanges(AGENT_MESSAGES))

        assert "First I will list the tables" not in rendered
        assert "There is an activity_log table" not in rendered

    def test_the_agents_own_prompt_and_question_are_left_out(self):
        """The question reaches the filter through [USER_QUESTION]; carrying it here too
        would put the same text in the prompt twice."""
        rendered = " ".join(sql + res for sql, res in runner._probe_exchanges(AGENT_MESSAGES))

        assert "careful SQL agent" not in rendered
        assert "[USER_QUESTION]" not in rendered

    def test_the_final_solution_is_left_out(self):
        """The final SQL is already shown split across [PREVIOUS_CTES], [CURRENT_CTE] and
        [DOWNSTREAM]; a second undivided copy would compete with that."""
        rendered = " ".join(sql + res for sql, res in runner._probe_exchanges(AGENT_MESSAGES))

        assert "<solution>" not in rendered

    def test_an_unanswered_probe_is_dropped(self):
        """Without a result there is nothing to learn from it."""
        pairs = runner._probe_exchanges([
            {"role": "assistant", "content": "<sql>SELECT 1</sql>"},
            {"role": "assistant", "content": "<think>no result came back</think>"},
        ])

        assert pairs == []

    def test_no_messages_means_no_pairs(self):
        assert runner._probe_exchanges([]) == []


class TestLoadingTheAgentHistory:
    """In the --refine-output path `_sync_instance_dirs` copies the whole instance
    directory into the arm, so messages.json sits beside the SQL being refined."""

    def test_it_reads_messages_json_from_the_instance_directory(self, tmp_path):
        (tmp_path / "messages.json").write_text(json.dumps(AGENT_MESSAGES), encoding="utf-8")

        pairs = runner._agent_probe_exchanges(tmp_path)

        assert len(pairs) == 3

    def test_a_missing_history_is_not_fatal(self, tmp_path):
        """Retrieval must still run; the filter simply gets no evidence section."""
        assert runner._agent_probe_exchanges(tmp_path) == []

    def test_an_unparseable_history_is_not_fatal(self, tmp_path):
        (tmp_path / "messages.json").write_text("{not json", encoding="utf-8")

        assert runner._agent_probe_exchanges(tmp_path) == []


# --- Ordering and metadata ---------------------------------------------------------

class TestOrderingAndSourceDatabase:
    """Fixing defect one. `_store_rule_index` already reads the store's db column for
    stage 8, so filling the gap needs no change to tkstore/tagger_index.py."""

    def test_every_rule_gets_its_source_database(self, store):
        ordered = runner._rules_for_context_filter(
            [_cand("26"), _cand("58", scope="db")], tkstore_path=store, db="bank_sales_trading")

        by_id = {r["mem_id"]: r for r in ordered}
        assert by_id["26"]["db"] == "California_Traffic_Collision"
        assert by_id["58"]["db"] == "bank_sales_trading"

    def test_rules_for_this_database_come_first(self, store):
        """The upstream bucketing intends this but never achieves it, because its test
        `rule.get('db') == db` compares None against the database name."""
        ordered = runner._rules_for_context_filter(
            [_cand("26"), _cand("42"), _cand("58", scope="db"), _cand("60", scope="db")],
            tkstore_path=store, db="bank_sales_trading")

        assert [r["mem_id"] for r in ordered[:2]] == ["58", "60"]

    def test_generic_rules_keep_their_relative_order(self, store):
        """Only the db-first split is introduced; reordering generic rules among themselves
        would add a second unmeasured change."""
        ordered = runner._rules_for_context_filter(
            [_cand("42"), _cand("26"), _cand("70")],
            tkstore_path=store, db="bank_sales_trading")

        assert [r["mem_id"] for r in ordered] == ["42", "26", "70"]

    def test_a_generic_rule_from_this_database_is_not_promoted(self, store):
        """Rule 70 was mined from bank_sales_trading but tagged generic, so it carries no
        schema anchor and does not earn the db-specific slot."""
        ordered = runner._rules_for_context_filter(
            [_cand("70"), _cand("58", scope="db")],
            tkstore_path=store, db="bank_sales_trading")

        assert [r["mem_id"] for r in ordered] == ["58", "70"]

    def test_the_candidates_passed_in_are_not_mutated(self, store):
        """`_retrieve_rules_for` reports the candidate list too, so it must not acquire
        fields as a side effect of building the prompt."""
        candidates = [_cand("26")]

        runner._rules_for_context_filter(candidates, tkstore_path=store, db="log")

        assert "db" not in candidates[0]

    def test_a_rule_the_store_cannot_explain_is_kept_and_marked(self, store):
        ordered = runner._rules_for_context_filter(
            [_cand("999")], tkstore_path=store, db="log")

        assert [r["mem_id"] for r in ordered] == ["999"]
        assert ordered[0]["db"] == "unknown"


# --- Prompt ------------------------------------------------------------------------

LOG_CTE = """WITH normalized_activity AS (
  SELECT session, stamp,
         CASE WHEN path LIKE '%/' AND LENGTH(path) > 1
              THEN SUBSTR(path, 1, LENGTH(path) - 1) ELSE path END AS normalized_path
  FROM activity_log
)"""

DOWNSTREAM_SQL = """-- CTE: session_land_exit
WITH session_land_exit AS (
SELECT DISTINCT session, normalized_path FROM normalized_activity
)

SELECT normalized_path, COUNT(DISTINCT session) FROM session_land_exit GROUP BY 1"""


def _prompt(store, **over):
    kwargs = dict(
        rules=runner._rules_for_context_filter(
            [_cand("26"),
             _cand("58", scope="db", table="transactions", column="txn_type")],
            tkstore_path=store, db="log"),
        user_question="For each user session, count the events before the first /detail click.",
        db="log",
        probe_exchanges=runner._probe_exchanges(AGENT_MESSAGES),
        previous_ctes="",
        current_sql=LOG_CTE,
        downstream=DOWNSTREAM_SQL,
        full_query=False,
    )
    kwargs.update(over)
    return runner._context_filter_prompt(**kwargs)


class TestTheSections:
    def test_all_sections_are_present(self, store):
        prompt = _prompt(store)

        for section in ("[USER_QUESTION]", "[DATABASE]", "[AGENT_PROBE_EVIDENCE]",
                        "[CURRENT_CTE]", "[DOWNSTREAM]", "[CANDIDATE_RULES]"):
            assert section in prompt, section

    def test_the_question_is_carried_verbatim(self, store):
        prompt = _prompt(store)

        assert "count the events before the first /detail click" in prompt

    def test_the_probe_evidence_carries_both_sql_and_results(self, store):
        prompt = _prompt(store)

        assert "SELECT DISTINCT path FROM activity_log;" in prompt
        assert "/detail/" in prompt, "the observed trailing-slash variants"

    def test_a_failed_probe_is_shown_too(self, store):
        prompt = _prompt(store)

        assert "no such column: bogus" in prompt

    def test_the_downstream_consumer_is_shown(self, store):
        """This is what tells the filter that deduplication already happens later, so a
        rule demanding it here is not catching a bug."""
        prompt = _prompt(store)

        assert "session_land_exit" in prompt

    def test_previous_ctes_are_omitted_when_there_are_none(self, store):
        prompt = _prompt(store, previous_ctes="")

        assert "[PREVIOUS_CTES]" not in prompt

    def test_previous_ctes_are_shown_when_present(self, store):
        prompt = _prompt(store, previous_ctes="WITH earlier AS (SELECT 1)")

        assert "[PREVIOUS_CTES]" in prompt
        assert "earlier" in prompt

    def test_the_evidence_section_is_omitted_when_there_is_none(self, store):
        prompt = _prompt(store, probe_exchanges=[])

        assert "[AGENT_PROBE_EVIDENCE]" not in prompt


class TestTheFinalSelectStage:
    """At that stage retrieval is handed the complete query, because that is what the
    refiner reviews there, so there is no downstream to show."""

    def test_the_fragment_is_labelled_as_the_full_query(self, store):
        prompt = _prompt(store, full_query=True, downstream="")

        assert "[FULL_QUERY]" in prompt
        assert "[CURRENT_CTE]" not in prompt

    def test_no_empty_downstream_section_is_emitted(self, store):
        prompt = _prompt(store, full_query=True, downstream="")

        assert "[DOWNSTREAM]" not in prompt


class TestTheRuleMetadata:
    def test_each_rule_shows_its_source_database(self, store):
        prompt = _prompt(store)

        assert "db=California_Traffic_Collision" in prompt
        assert "db=all" not in prompt, "this was the bug: every rule rendered as db=all"

    def test_table_and_column_are_shown(self, store):
        """For db-scoped rules these are real anchors -- 56 of 66 carry a table and 53 a
        column. For generic rules they read 'all', and that is itself evidence that the
        rule has no schema anchor."""
        prompt = _prompt(store)

        assert "table=transactions" in prompt
        assert "column=txn_type" in prompt

    def test_operations_data_type_and_nulls_are_shown(self, store):
        prompt = _prompt(store)

        assert "operations=select, case" in prompt
        assert "data_type=all" in prompt
        assert "nulls=all" in prompt

    def test_rules_are_numbered_from_one(self, store):
        """The model answers with these numbers, so the mapping back has to be unambiguous."""
        prompt = _prompt(store)

        assert "[1]" in prompt and "[2]" in prompt

    def test_the_rule_text_is_included(self, store):
        prompt = _prompt(store)

        assert "rule text 26" in prompt


class TestTheSelectionInstructions:
    """High recall is kept: the filter is a retrieval step, and round H's harm came from
    rules being applied badly rather than from too many being offered."""

    def test_inclusion_is_the_default(self, store):
        prompt = _prompt(store).lower()

        assert "when in doubt" in prompt or "err on the side of inclusion" in prompt

    def test_it_does_not_ask_for_proof_of_relevance(self, store):
        prompt = _prompt(store)

        assert "not exclude" in prompt.lower() or "do not exclude" in prompt.lower()

    def test_the_four_exclusion_conditions_are_stated(self, store):
        prompt = _prompt(store).lower()

        assert "upstream" in prompt or "downstream" in prompt
        assert "contradict" in prompt or "disprove" in prompt
        assert "does not exist" in prompt or "absent" in prompt

    def test_a_foreign_source_database_is_not_by_itself_grounds_for_exclusion(self, store):
        """Cross-database transfer is the point of the method; only a rule whose
        applicability depends on its source schema should go."""
        prompt = _prompt(store).lower()

        assert "not exclude a rule merely because" in prompt or \
               "not solely because it was mined" in prompt

    def test_the_ordering_claim_matches_what_was_actually_done(self, store):
        """Upstream tells the model db-specific rules come first while its bucketing is
        dead. Here the ordering is real, so the claim may be made."""
        prompt = _prompt(store)

        assert "log" in prompt
        ordered_ids = [r["mem_id"] for r in runner._rules_for_context_filter(
            [_cand("26"), _cand("58", scope="db")], tkstore_path=store, db="bank_sales_trading")]
        assert ordered_ids[0] == "58"

    def test_the_output_format_is_specified(self, store):
        prompt = _prompt(store)

        assert "selected_indices" in prompt
        assert "reasoning" in prompt
