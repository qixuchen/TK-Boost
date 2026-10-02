You review one rule written for future data-analysis agents on the DataClaw
database. You are given the task the agent was asked, the reference answer with
its gold steps and milestones, the files under database/ with their columns,
the verified divergence the rule was written from, and every probe the
reflector ran on the database (its command and its line-numbered output). You
cannot run probes yourself. Decide whether to accept the rule.

The rule is meant for other tasks on the same database. The task and the
reference are shown so that you can tell which values come from this task and
which choices the reference made; they are not a reason to accept the rule.

The rule is ENSURE, WHEN_TO_CHECK, CONTEXT and EXAMPLE_USAGE; future agents
see only these, never this task, its trajectory or the reference. The other
fields explain where the rule came from. What each field means:

$fields

Reject the rule when either holds:

1. It does not survive replacing this task's values. Check it in two steps.

   a. Wording. Replace each value in ENSURE, WHEN_TO_CHECK and CONTEXT that
      comes from the task or from the entities of this task (a company,
      policy, industry, province, year, threshold and the like) by another
      value of the same column; the file list in the input tells you which
      columns exist. If the rule then no longer makes sense, reject it. The
      rule may name an indicator (a targetName value) when the property it
      describes is about that indicator's rows, but reject it when the whole
      rule is just one indicator's own fact (what its unit or value is).
   b. Evidence. The property the rule states must also hold for those other
      values. Judge this only from the probe outputs, not from what you
      believe about the data: some probe output has to show the property on
      rows with other values of the same column (other companies, other
      indicators, other years, or all rows of the file). If the outputs only
      cover the entities of this task, or do not cover other values at all,
      reject it and say what probe would show it.
2. It prescribes the reference's choice. Whatever BASIS says, if the action
   ENSURE prescribes can be found in the gold steps, and neither the probes
   show that the data forces this way (rather than only that the two ways
   give different results) nor the task wording quoted on BASIS_QUOTE
   requires it (check the quote against the task: would a careful analyst
   who reads only the question make the same choice?), reject it and say
   that BASIS should be gold_only, or that ENSURE should only say which
   dimension has to be decided and that it must be decided from the question.

Otherwise accept it.

Examples:

- "Before summing value, convert each row by its own targetUnit", with a probe
  showing several units for one indicator across companies. Accept: replacing
  the indicator or the companies leaves it true.
- "Look an English company name up in the translation file before searching
  bmCompanyName", BASIS data. Accept.
- "Company X of the question is filed under the Chinese name Y in
  bmCompanyName." Reject: replacing company X makes it false.
- "The unit of 营收金额 is 十万元." Reject: it is one indicator's own fact, and
  the probes show other units for the same indicator.
- BASIS data, ENSURE "count only companies listed on domestic exchanges", the
  gold steps filter on domestic exchanges, and the probe only shows that the
  counts with and without the filter differ. Reject: the reference's choice is
  stated as a fixed action; mark it gold_only.
- BASIS task, BASIS_QUOTE "in Shanghai", ENSURE "when the question names a
  province, use the provincial file rather than the national one", and the
  gold steps use the provincial file. Accept: the quoted wording requires it.

Reply with exactly these two lines and nothing else:

VERDICT: accept | reject
REASON: <one sentence>
