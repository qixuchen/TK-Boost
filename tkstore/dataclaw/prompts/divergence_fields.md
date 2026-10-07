- DIVERGENCE: what the analysed agent did, and at which of its commands. A<n>
  numbers the agent's commands in its trajectory; it is unrelated to the
  reflector's probes P1, P2, ...
- NEEDED: what this task needed instead, in terms of files, columns and
  operations. It is stated for this task only.
- BASIS: who requires what NEEDED describes. One of:
  - data: the data forces it, whatever the question says. Without it the
    correct values cannot be obtained.
  - task: the wording of the question decides it; a differently worded
    question would need a different action.
  - gold_only: neither the data nor the question forces it; the reference
    answer simply made this choice. Another task's reference may choose
    differently.
- BASIS_QUOTE: the words of the task that require NEEDED, copied verbatim from
  the task. Given only when BASIS is task.
- INSTANCE: what the reflector saw on the entities of this task (company names,
  the province, the values the agent and the reference got). It supports the
  reasoning and is not part of the rule.
- SCOPE: derived by the harness from TABLES and COLUMNS: generic (no file),
  file (whole files), column, multi_column (several columns of one file) or
  cross_table (several files).
- TABLES: the files under database/ that the fact is about as a whole.
- COLUMNS: the columns the fact is about, written <file>.<column>.
- FACT: the data understanding the agent was missing: a property of those files
  and columns that should hold across their rows, not only for this task's
  entities.
- CATEGORY: the kind of data property the fact is, picked from a fixed list of
  hints (units, several names for one indicator, coverage gaps, ...) or Other.
- EVIDENCE: output lines of the reflector's probes that show the fact on this
  task. P<n>:L<a> means line a of the output of probe n (P<n>:L<a>-L<b> a range
  of lines); the text after the arrow is those lines, copied by the harness.
- GENERALITY: output lines, in the same P<n>:L<a> form, that the reflector cites
  to show the fact beyond the entities of this task.
- REPRODUCED: a claim that a probe computed a milestone the agent missed: the
  milestone key, its gold value, the value the reflector reports, and the
  output line that prints it (P<n>:L<a>, with the line copied by the harness).
  The harness has already checked that the value is on that line and equals
  the gold value (numbers within 1%).
- SEMANTIC_MATCH: optional; the reflector's explanation of why a differently
  worded value means the same as the gold value.
- ENSURE: the action of the rule: what a future agent should do.
- WHEN_TO_CHECK: the kind of question in which a future agent should apply the
  rule, with this task's entities and values abstracted away.
- TRIGGER: the phrase of this task that made the knowledge necessary. It may
  name entities; it is kept for review only and is not shown to future agents.
- CONTEXT: why the rule holds: the data property behind it, which one probe
  could verify.
- EXAMPLE_USAGE: right versus wrong handling with concrete values, illustrating
  the rule.
