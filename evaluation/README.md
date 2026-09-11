# Evaluation

This page explains how to reproduce the paper's Spider2 evaluation results from TK-Boost outputs.

## Setup

Before running evaluations, you need to set up the required files from the Spider2 repository:

### 1. Gold Results

Copy the gold evaluation results from the Spider2 repository:

```bash
# Clone Spider2 repo if you haven't already
git clone https://github.com/xlang-ai/Spider2.git

# Copy gold directory to this evaluation folder
cp -r Spider2/spider2-lite/evaluation_suite/gold evaluation/
```

The `gold/` directory should contain:
- `exec_result/*.csv` - Gold execution results for each instance
- `sql/*.sql` - Gold SQL queries, one file per instance

Note that the gold SQL lives in `gold/sql/`, not alongside the CSVs in
`gold/exec_result/`. Populate (Algorithm 2) reads `gold/sql/<instance_id>.sql`.

### 2. Evaluation Standards File

The `cp` above already places `spider2lite_eval.jsonl` inside `gold/`, which is
where `evaluate.py` looks for it (`--gold_dir` + `/spider2lite_eval.jsonl`). Copy
it explicitly only if your `gold/` is missing it:

```bash
cp Spider2/spider2-lite/evaluation_suite/gold/spider2lite_eval.jsonl evaluation/gold/
```

This file contains evaluation metadata for each instance (condition columns, ignore order flags, etc.).

### 3. Databases

The evaluation itself does not open the databases, but generating predictions
does. See the repository README for `SPIDER2_DB_ROOT`.

## Usage

### Evaluate All Outputs

```bash
python evaluation/evaluate.py \
  --mode exec_result \
  --result_dir outputs \
  --gold_dir evaluation/gold
```

### Evaluate Single Instance

Pass one instance directory produced by the runner. Its name must keep the
`<instance_id>_YYYYMMDD_HHMMSS` form, since that is how the instance id is
recovered:

```bash
python evaluation/evaluate.py \
  --mode exec_result \
  --result_dir outputs/local007_20260910_172039 \
  --gold_dir evaluation/gold
```

Substitute a directory that actually exists under `outputs/`; the timestamp is
assigned at run time. If nothing matches, the evaluator now reports which
directory it looked at and why it found no instances instead of failing with
`KeyError: 'score'`.

## Output Files

The evaluation script creates:
- `evals.csv` - Detailed results for each instance (instance_id, score, score_final, assistant_turns, base, final, either, both)
- `correct_ids.csv` - List of instances with perfect scores
- `correct_ids_final.csv` - List of instances with perfect final scores

## Scoring

- **score** (base): Evaluation of `execution_result.csv`
- **score_final**: Evaluation of `execution_result_final.csv` (falls back to base if not present)
- **either**: Instance scored correctly in at least one version
- **both**: Instance scored correctly in both versions

Scores are 0 (incorrect) or 1 (correct).
