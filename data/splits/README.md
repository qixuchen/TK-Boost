# Spider2-SQLite train / test split

| File | Instances |
| --- | --- |
| `spider2_sqlite_train.txt` | 34 |
| `spider2_sqlite_test.txt` | 101 |
| total | 135 |

## Why this exists

Populate (Algorithm 2) reads gold SQL and gold execution results, so it sees the
answers. Running populate on an instance and then evaluating on that same
instance would leak. The split is therefore fixed **before** any populate run and
committed, so experiments stay comparable.

## How it was generated

```bash
python scripts/make_splits.py --seed 0 --train-size 34
```

Population: the 135 instance ids in `data/spider2-lite.jsonl` whose id starts with
`local`, which is what `src/utils/agent_utils.infer_engine` treats as SQLite.

Sizes follow the paper's Table 11 (135 total, 34 train, 101 test). **The instance
ids are ours, not the paper's** — Table 11 reports only the counts, so the exact
split cannot be reproduced from the paper. Any comparison against the published
numbers has to state this.

The split is not stratified by database. The paper does not describe stratifying,
and adding it would introduce a difference that makes results harder to compare.

Do not edit these files by hand; regenerate with the command above.

## Database coverage

Whether a db-scoped rule learned during populate can help at evaluation time
depends on the test instance's database also appearing in the train split.

| Measure | Value |
| --- | --- |
| Databases with at least one train instance | 21 / 30 |
| Databases present in both splits | 18 |
| Test instances whose database has a train instance | 77 / 101 (76%) |

That 76% is an upper bound on how far db-scoped rules can reach; the remaining 24
test instances can only benefit from generic rules. The coverage is this high
because instances are unevenly distributed across databases — several databases
carry 7–9 instances each, so a random sample of 34 lands in them with high
probability.
