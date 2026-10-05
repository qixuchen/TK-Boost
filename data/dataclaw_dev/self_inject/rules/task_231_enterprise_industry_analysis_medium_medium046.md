[DATABASE NOTES]
The notes below come from earlier analyses of this same database. They describe
pitfalls in the data files, not answers to the question above. For each note:
1. Read "Applies when". If it does not describe the question above, ignore the note.
2. If it applies, carry out "Check" before computing the quantity it concerns.
3. "Why" states the data property behind the note; verify it in the data if in doubt.
4. If the question explicitly requires something different, follow the question.

Note 1
Applies when: The question asks for an industry-level median operating profit and does not unambiguously state whether it wants a national value or a specific province’s value.
Check: Before fixing a single median operating profit figure for an industry, explicitly check both industry/national_industry_status.csv and industry/regional_industry_status.csv for that industry and for targetName values containing “营业利润” and “中位数”; decide from the task wording whether to use the national (no province column) or a provincial row, and only then convert from each row’s own targetUnit to the desired unit.
Why: The database holds two parallel summary tables: industry/national_industry_status.csv, where rows are keyed only by industry and year, and industry/regional_industry_status.csv, where rows additionally include province; both contain median-like indicators for “营业利润…中位数” but with differing scopes and units, so automatically taking only the provincial or only the national table can silently change the metric.
[END DATABASE NOTES]

The answer must still follow the output guidelines in the question above.
