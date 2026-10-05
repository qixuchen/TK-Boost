[DATABASE NOTES]
The notes below come from earlier analyses of this same database. They describe
pitfalls in the data files, not answers to the question above. For each note:
1. Read "Applies when". If it does not describe the question above, ignore the note.
2. If it applies, carry out "Check" before computing the quantity it concerns.
3. "Why" states the data property behind the note; verify it in the data if in doubt.
4. If the question explicitly requires something different, follow the question.

Note 1
Applies when: The question requires the number of enterprises in an industry, or an enterprise count in an industry broken down by ownership/exchange, and does not explicitly specify whether to use national statistics or the full set of listed companies.
Check: When a question asks for an industry-level enterprise count, check both the summary in industry/national_industry_status.csv (rows whose targetName ends with “企业数量”) and the raw count from enterprise/company_profile.csv for that industry; observe that they may differ and then decide, based on the question’s intent, which population (summary vs full roster) should be used rather than assuming one source is always correct.
Why: The database provides both a detailed company roster (enterprise/company_profile.csv) and aggregated national industry statistics (industry/national_industry_status.csv). For a given industry, the enterprise counts in these two files can diverge (e.g., 金融业 summary value 297 vs raw 305 in P1:L4-L5), reflecting different inclusion criteria. A careful analyst must notice this divergence and choose the appropriate source according to the task, rather than silently treating one file’s values as the only truth.
[END DATABASE NOTES]

The answer must still follow the output guidelines in the question above.
