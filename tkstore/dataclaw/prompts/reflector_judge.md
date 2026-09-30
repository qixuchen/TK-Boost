You review one rule written for future data-analysis agents on the DataClaw
database, together with the verified divergence it was written from and the
probes cited to show that it is general. Decide whether to accept the rule.

Reject the rule when either holds:

1. It holds only for individual entities. The rule is, in substance, a fact
   about one or a few specific companies, policies, industries, provinces or
   indicator names (what a company is called, which industry it is in, what one
   indicator's unit or value is), even if it is phrased in terms of columns; or
   the GENERALITY probes only show the property on the entities of this task.
   A general rule describes a property of files and columns that holds across
   their rows: it still holds when the concrete values are replaced by other
   values of the same columns.
2. BASIS is gold_only and ENSURE states the reference's choice as a fixed
   action. For gold_only, ENSURE may only say which dimension has to be decided
   and that it must be decided from the question.

Otherwise accept it.

Examples:

- "Before summing value, convert each row by its own targetUnit", with a probe
  showing several units for one indicator across companies. Accept.
- "Look an English company name up in the translation file before searching
  bmCompanyName", BASIS data. Accept.
- "Company X of the question is filed under the Chinese name Y in
  bmCompanyName." Reject: it is one company's name.
- BASIS gold_only, ENSURE "count only companies listed on domestic exchanges".
  Reject: the reference's choice is stated as a fixed action.

Reply with exactly these two lines and nothing else:

VERDICT: accept | reject
REASON: <one sentence>
