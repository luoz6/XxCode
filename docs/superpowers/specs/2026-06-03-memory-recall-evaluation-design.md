# Deterministic Memory Recall Evaluation Design

> Version 0.1 | 2026-06-03 | Scope: define an SDD-first, TDD-implemented evaluation framework for memory recall quality and stability

## 1. Objective

The current memory test suite verifies correctness of individual behaviors such
as parsing, filtering, index loading, and missing-file handling. What it does
not yet provide is a deterministic way to answer these product questions:

- how good is memory recall on a fixed benchmark
- how stable is the recall result under controlled perturbations
- whether one implementation is measurably better or worse than another

This design adds a deterministic evaluation layer for the recall stage only.
The first version measures:

- recall quality
- recall stability

It does not cover extraction quality, long-horizon memory consistency, or
end-to-end conversational impact.

## 2. Current State

The current recall path lives mainly in `src/xxcode/memory/recall.py`.

Important runtime pieces:

- `select_relevant_memories(...)`
- `recall_memories_for_query(...)`
- `parse_memory_index(...)`
- `load_memory_index(...)`

Existing tests already cover:

- response parsing from the selector model
- cap and invalid-name filtering
- exclusion of already surfaced files
- end-to-end loading from `MEMORY.md`
- missing files and fallback behavior

These tests prove functional correctness, but they do not produce benchmark
values such as `precision`, `recall`, `F1`, or stability scores. As a result,
the project cannot compare two recall implementations with concrete numbers.

## 3. Problem To Solve

The live selector inside `select_relevant_memories(...)` is model-based and
therefore unsuitable as the primary source of deterministic evaluation.

At the same time, a trivial fixed-answer mock is also insufficient, because it
would make every curated case score perfectly and would measure only plumbing
rather than recall behavior.

The missing piece is a deterministic evaluation selector that:

1. runs offline
2. returns the same result on every run
3. is strong enough to distinguish better and worse recall inputs
4. exercises the real recall pipeline rather than bypassing it

## 4. Design Goals

This design should:

1. keep the first evaluation suite completely deterministic and CI-safe
2. measure both quality and stability of the recall stage
3. reuse the production recall pipeline as much as possible
4. avoid dependence on live API calls or model sampling
5. surface concrete numeric results per case and per suite
6. fit cleanly into TDD red-green-refactor implementation

## 5. Non-Goals

This design does not:

- evaluate the extraction subsystem
- score long-term consistency across sessions
- judge the final assistant answer quality
- benchmark real LLM semantic retrieval
- change the runtime product behavior in this phase

## 6. Chosen Evaluation Strategy

The chosen strategy is a deterministic mixed evaluation framework:

- quality benchmark cases with gold labels
- stability benchmark cases with controlled perturbations

The suite uses a deterministic selector client rather than a live model.
Instead of returning a hardcoded gold answer, the selector computes a stable
ranking from the query and the `MEMORY.md` manifest. This preserves meaningful
signal: if the index descriptions, filenames, or organization are weak, the
score will degrade.

This is intentionally a surrogate for live semantic retrieval, not a claim that
the deterministic selector is production-equivalent. Its purpose is to provide
a frozen yardstick for regression testing and version comparison.

## 7. Deterministic Selector Contract

### 7.1 Why a Scoring Selector Is Required

A fixed-output mock would trivialize the benchmark:

- `precision = 1.0`
- `recall = 1.0`
- `F1 = 1.0`

for every case, regardless of memory quality. That would be useless.

Therefore the evaluation suite needs a deterministic selector that derives its
answer from the benchmark input.

### 7.2 Selector Behavior

The deterministic selector should inspect:

- the user query
- each manifest line from `MEMORY.md`
- optionally the indexed title, filename stem, and description

It should assign a stable lexical score to each indexed entry. The exact
formula may evolve during implementation, but version 0.1 assumes:

1. tokenize query and manifest text into lowercase terms
2. score term overlap between query and each entry
3. allow title and description tokens to contribute more than filename tokens
4. break ties deterministically by filename
5. return up to `MAX_RECALLED_MEMORIES`

This selector is implemented only for tests. Production recall continues to use
the existing model-backed path.

### 7.3 Injection Strategy

The evaluation should reuse `select_relevant_memories(...)` and
`recall_memories_for_query(...)` without changing their public behavior.

The selector is injected through the existing `client_factory` contract by
providing a deterministic test client whose `complete(...)` method returns a
JSON array of selected filenames derived from the prompt content.

This keeps the benchmark close to the real pipeline:

- prompt assembly still happens
- manifest parsing still happens
- JSON parsing still happens
- valid-name filtering still happens
- file loading still happens

## 8. Benchmark Dataset Design

### 8.1 Fixture Location

The curated benchmark dataset should live under:

- `tests/memory/fixtures/recall_eval/`

### 8.2 Case Shape

Each case should define:

- `case_id`
- `query`
- `memory_index`
- `memory_files`
- `expected_recall`
- optional `expected_top1`
- optional perturbation variants

The benchmark representation may be JSON, YAML, or inline Python structures.
The important requirement is that it remains human-reviewable and deterministic.

### 8.3 Case Categories

The initial suite should include at least these case categories:

1. single obvious target
2. two related relevant memories
3. one relevant memory plus several distractors
4. filename looks relevant but description is misleading
5. description is relevant but filename is generic
6. cap pressure where more than five candidates look plausible

These cases should be intentionally small and curated. The goal is not scale in
version 0.1; the goal is signal clarity.

## 9. Metric Contract

### 9.1 Quality Metrics

Each quality case should report:

- `precision_at_k`
- `recall_at_k`
- `f1_at_k`
- `top1_hit`
- `topk_full_match`

Definitions:

- `precision_at_k = matched / returned`
- `recall_at_k = matched / expected`
- `f1_at_k = harmonic_mean(precision_at_k, recall_at_k)`
- `top1_hit = 1.0` when the first selected file matches `expected_top1`,
  otherwise `0.0`
- `topk_full_match = 1.0` when the selected filename set exactly matches the
  expected filename set, otherwise `0.0`

`k` is fixed to `MAX_RECALLED_MEMORIES` in version 0.1.

### 9.2 Stability Metrics

Each stability case should report:

- `repeat_consistency`
- `order_stability`
- `noise_resistance`
- `description_robustness`

Definitions:

- `repeat_consistency`: exact selected list equality across repeated runs of the
  same case
- `order_stability`: selected set equality after reordering `MEMORY.md` lines
- `noise_resistance`: selected set preservation after adding irrelevant indexed
  memories
- `description_robustness`: selected set preservation after non-target
  description rewrites that should not change the answer

Version 0.1 compares filenames only for set-based stability metrics. Order is
assessed separately.

### 9.3 Aggregate Reporting

The suite should expose two aggregate scores instead of one blended score:

- `quality_score`
- `stability_score`

Recommended version 0.1 formula:

- `quality_score = average(f1_at_k, top1_hit, topk_full_match)`
- `stability_score = average(repeat_consistency, order_stability,
  noise_resistance, description_robustness)`

The suite should also retain raw metric fields in the report so future analysis
does not depend on only two aggregates.

## 10. Pass/Fail Policy

The evaluation layer should serve two purposes:

1. produce readable benchmark values
2. fail on material regressions

Version 0.1 should use conservative deterministic assertions:

- each curated case may assert exact selected filenames when the benchmark is
  meant to lock behavior
- aggregate suite tests should assert minimum thresholds rather than a single
  frozen total score

This split avoids an overly brittle suite while still making regressions
actionable.

Recommended initial threshold style:

- `quality_score >= floor`
- `stability_score == 1.0` for perturbations that should be invariant under the
  deterministic selector

Exact floors should be set during TDD once the curated corpus is in place.

## 11. Component Design

The evaluation framework should remain test-local.

### 11.1 Test Modules

Add:

- `tests/memory/test_recall_eval.py`
- `tests/memory/test_recall_stability.py`

### 11.2 Test Helpers

If helper code becomes non-trivial, add:

- `tests/memory/helpers/recall_eval.py`

Responsibilities:

- load curated cases
- materialize a temporary memory directory
- create the deterministic selector client
- run recall
- compute per-case metrics
- compute aggregate suite metrics
- render a compact summary for pytest output

### 11.3 No Production-Only Scoring Types in This Phase

Unless implementation friction proves otherwise, evaluation-specific score
objects should stay under `tests/` rather than `src/xxcode/`. This keeps phase
one tightly scoped to testable measurement rather than product API design.

## 12. Data Flow

For each benchmark case:

1. materialize a temporary memory directory
2. write the case memory files
3. write the case `MEMORY.md`
4. inject the deterministic selector client via `client_factory`
5. call `recall_memories_for_query(...)`
6. collect selected filenames
7. compute quality or stability metrics
8. aggregate and print a compact report

For perturbation cases:

1. run the baseline case
2. apply one controlled perturbation
3. rerun with the same query
4. compare baseline and perturbed results according to the target metric

## 13. Error Handling And Failure Diagnostics

The evaluation suite must fail loudly and specifically when the benchmark setup
is invalid.

Required validation:

- expected filenames must exist in the case memory directory
- expected filenames must be present in the case `MEMORY.md`
- perturbation cases must preserve benchmark invariants other than the intended
  perturbation

Failure output should include:

- `case_id`
- query
- expected filenames
- actual filenames
- metric values
- perturbation type when applicable

The goal is that a regression report is readable directly from pytest output.

## 14. Testing Strategy

This design explicitly requires TDD red-green-refactor during implementation.

Recommended order:

1. add the smallest failing quality benchmark case
2. make the deterministic selector and loader pass that case
3. add aggregate metric computation
4. add one stability perturbation at a time
5. only then generalize helpers and refactor duplication

The first implementation should stay minimal. Fancy reporting, corpus growth,
and optional scripts can come later.

## 15. SDD And TDD Process Constraints

This design is intentionally process-constraining:

- SDD first: the benchmark contract is written and reviewed before code changes
- TDD second: every implementation step starts with a failing test

That means the implementation plan should be organized around red-green-refactor
tasks such as:

- write a failing benchmark case
- run it and confirm failure
- add minimal helper code
- rerun until green
- refactor without changing benchmark outcomes

## 16. Implementation Boundaries For Phase One

Phase one may modify production code only when needed to make deterministic
evaluation possible without distorting runtime behavior. Any such change should
be narrowly scoped and justified.

Preferred approach:

- keep changes in tests only

Fallback approach if needed:

- add small dependency-injection seams that preserve default runtime behavior

This boundary matters because the current goal is measurement, not redesign of
the recall subsystem.

## 17. Success Criteria

This design is successful when the repository gains a deterministic recall
benchmark that can answer all of the following with concrete numbers:

- what is the current recall quality score
- what is the current recall stability score
- which cases are weak
- whether a code change improved or regressed recall on the fixed corpus

## 18. Deferred Work

Later phases may extend beyond this design to cover:

- extraction quality
- long-term consistency across turns or sessions
- context organization quality of `MEMORY.md`
- live-model or end-to-end conversational evaluation

Those are intentionally deferred so the first benchmark can stay small,
deterministic, and shippable.
