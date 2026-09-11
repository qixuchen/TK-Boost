# Spider2-SQLite train / test split

| File | Instances |
| --- | --- |
| `spider2_sqlite_train.txt` | 24 |
| `spider2_sqlite_test.txt` | 111 |
| total | 135 |
| `spider2_sqlite_test_no_reference_leak.txt` | 86 (a subset of test) |

## Why this exists

Populate (Algorithm 2) reads gold SQL and gold execution results, so it sees the
answers. Running populate on an instance and then evaluating on that same instance
would leak. The split is therefore fixed **before** any populate run and committed,
so experiments stay comparable.

## Why the split is by gold SQL availability, not random

Algorithm 3 needs the gold SQL `s*`: its line 5 is
`MakeCorrection(q, D, s, s*, R, R*)`. But Spider2-lite publishes gold SQL for only
**24 of the 135** SQLite instances (17.8%), while publishing gold execution
*results* for all 135. Upstream `Spider2` was checked directly; there is no other
copy, and `spider2-lite.jsonl` carries only `instance_id / question / db /
external_knowledge`.

So the split follows the data: instances with gold SQL become train, the rest
become test. Two consequences worth stating in any write-up:

- **Evaluation is unaffected.** `evaluation/evaluate.py` compares against
  `evaluation/gold/exec_result/*.csv`, which covers 135/135. All 111 test instances
  are evaluable.
- **Train is 24, not the paper's 34.** The paper's authors used gold SQL that is not
  public: of the 32 instances their released store was trained on, only 7 have
  public gold SQL.

The split is not stratified by database. The paper does not describe stratifying,
and adding it would introduce a difference that makes results harder to compare.

## The third file

`spider2_sqlite_test_no_reference_leak.txt` holds the 86 test instances that the
upstream reference store `tkstore/tkstore_sqlite.csv` was **not** trained on. That
store was populated over 32 instances; 7 of them are in our train, and the other 25
fall in our test, so evaluating that store on the full test set would be testing on
its own training data.

Use this subset only when evaluating the upstream store as a reference point. Our
own store, populated from the 24 train instances, is evaluated on the full 111.

## How it was generated

```bash
python scripts/make_splits.py
```

Population: the 135 instance ids in `data/spider2-lite.jsonl` whose id starts with
`local`, which is what `src/utils/agent_utils.infer_engine` treats as SQLite.

The output is deterministic — there is no seed, since membership is decided by
which gold SQL files exist. Do not edit these files by hand; regenerate with the
command above.

## Database coverage

Whether a db-scoped rule learned during populate can help at evaluation time
depends on the test instance's database also appearing in the train split.

| Measure | Value |
| --- | --- |
| Databases with at least one train instance | 16 / 30 |
| Databases present in both splits | 14 |
| Test instances whose database has a train instance | 65 / 111 (59%) |

That 59% is an upper bound on how far db-scoped rules can reach; the remaining 46
test instances can only benefit from generic rules. Coverage is lower than the
earlier seeded split gave (76%) because train membership is now dictated by gold
SQL availability rather than sampled, so it cannot preferentially land in the
databases that carry the most instances.
