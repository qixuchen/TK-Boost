[DATABASE NOTES]
The notes below come from earlier analyses of this same database. They describe
pitfalls in the data files, not answers to the question above. For each note:
1. Read "Applies when". If it does not describe the question above, ignore the note.
2. If it applies, carry out "Check" before computing the quantity it concerns.
3. "Why" states the data property behind the note; verify it in the data if in doubt.
4. If the question explicitly requires something different, follow the question.

Note 1
Applies when: The question is about “上市企业” of an industry (not a single exchange) and does not explicitly limit the venue (e.g. only 沪深); the population could reasonably include multiple listing venues for domestic firms.
Check: Before fixing the universe of listed companies for any aggregate (counts or CR ratios), inspect both enterprise/company_profile.csv.companyType and enterprise/company_profile.csv.country to see how many firms are domestic vs overseas and how many venues they list on; decide from the question whether to include all country=="中国" firms (across 沪深 and 港股) or only a subset, rather than defaulting to companyType=="沪深".
Why: enterprise/company_profile.csv shows 专用设备制造业 companies spread over several companyType values (沪深, 港股, 国外) and several country values (中国, 中国香港, 美国等); counts of firms and downstream CR20 calculations change materially depending on whether 港股,中国 firms are included, so silently equating “上市企业” with companyType=="沪深" yields biased aggregates.
[END DATABASE NOTES]

The answer must still follow the output guidelines in the question above.
