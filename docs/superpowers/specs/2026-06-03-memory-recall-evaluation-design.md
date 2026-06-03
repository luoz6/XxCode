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
- the indexed filename stem and description carried by each manifest line

It should assign a stable lexical score to each indexed entry. The exact
formula may evolve during implementation, but version 0.1 assumes:

1. tokenize query and manifest text into lowercase terms
2. score term overlap between query and each entry
3. allow description tokens to contribute more than filename tokens
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

### 7.4 Message-Format Coupling

The deterministic selector is intentionally coupled to the current user-message
shape produced by `select_relevant_memories(...)`.

In the current implementation, the selector prompt embeds the candidate list in
the user message under an `Available memories:` section followed by manifest
lines like:

- `- [indexed] filename.md: description`

The deterministic test client must parse that section from the assembled user
message in order to reconstruct the candidate set. This coupling should be
explicit in the benchmark because a future prompt-format change could break the
selector without changing the production recall semantics.

Accordingly, evaluation tests should fail clearly when the expected
`Available memories:` section cannot be found.

## 8. Benchmark Dataset Design

### 8.1 Fixture Location

For version 0.1, the curated benchmark dataset should live as inline Python
case definitions in the test helper module:

- `tests/memory/helpers/recall_eval.py`

This is the current default, not a temporary exception. The corpus is small, and
keeping it inline keeps the evaluation easy to review beside the deterministic
selector and scoring code.

If the corpus grows enough to need file-level review, versioning, or external
generation, migrate the same case contract to:

- `tests/memory/fixtures/recall_eval/`

### 8.2 Case Shape

Each case should define:

- `case_id`
- `query`
- `index_content: str`
- `memory_files: dict[str, str]`
- `expected_filenames: set[str]`
- optional `expected_top1: str`

The benchmark representation may be JSON, YAML, or inline Python structures.
The important requirement is that it remains human-reviewable and deterministic.

Field meanings:

- `index_content`: the literal content that will be written to `MEMORY.md`; this
  aligns with the `load_memory_index()` return shape and avoids splitting
  terminology between "memory index" and `MEMORY.md`
- `memory_files`: mapping of `filename -> full markdown file content`; helpers
  should write these contents directly into the temporary memory directory
- `expected_filenames`: the gold filename set for set-based quality assertions;
  version 0.1 does not assign full-order meaning to this field
- `expected_top1`: optional first-result expectation for cases that care about
  ranking, not just set membership

Version 0.1 treats `expected_filenames` as an unordered set. If a serialized
fixture format lacks a native set type, the loader may normalize an equivalent
list representation into a set during case loading, but the semantic contract
remains set-based.

Curated version 0.1 fixtures should also keep `index_content` below the
`MEMORY.md` truncation thresholds enforced by the runtime index loader. In
practice that means benchmark fixtures should stay under both:

- `MAX_ENTRYPOINT_LINES`
- `MAX_ENTRYPOINT_BYTES`

### 8.3 Perturbation Generation Policy

Version 0.1 perturbations should be generated programmatically by test helpers
from a baseline case. Baseline cases do not need to define explicit perturbation
variants.

Programmatic perturbations should cover:

1. `order_stability`: reorder lines inside `index_content`
2. `noise_resistance`: append irrelevant indexed memories and corresponding
   memory files
3. `description_robustness`: rewrite non-target descriptions while preserving
   the baseline answer expectation

This keeps the fixture schema small and avoids duplicating mostly identical case
data.

### 8.4 Case Categories

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
- `recall_at_k = matched / expected_count`
- `f1_at_k = harmonic_mean(precision_at_k, recall_at_k)`
- `top1_hit = 1.0` when the first selected file matches `expected_top1`,
  otherwise `0.0`
- `topk_full_match = 1.0` when the selected filename set exactly matches the
  expected filename set, otherwise `0.0`

If no files are selected and the case defines `expected_top1`, `top1_hit` is
defined as `0.0`.

If a case does not define `expected_top1`, `top1_hit` is undefined for that case
and must be excluded from the `top1_hit_rate` denominator. Such a case should
not count as either a top1 success or a top1 failure.

`k` is fixed to `MAX_RECALLED_MEMORIES` in version 0.1.

### 9.2 Stability Metrics

Each stability case should report:

- `repeat_consistency`
- `order_stability`
- `noise_resistance`
- `description_robustness`

Definitions:

- `repeat_consistency`: exact selected list equality across repeated runs of the
  same case; version 0.1 defines the repeat count as `N = 2`
- `order_stability`: selected set equality after reordering `MEMORY.md` lines
- `noise_resistance`: selected set preservation after adding irrelevant indexed
  memories
- `description_robustness`: selected set preservation after non-target
  description rewrites that should not change the answer

Version 0.1 compares filenames only for set-based stability metrics. Order is
assessed separately.

### 9.3 Aggregate Reporting

Version 0.1 should expose a scorecard, not a single blended `quality_score` or
`stability_score`.

Recommended quality scorecard fields:

- `n_cases`
- `n_top1_cases`
- `mean_precision_at_k`
- `mean_recall_at_k`
- `mean_f1_at_k`
- `top1_hit_rate`
- `full_match_rate`

Recommended stability scorecard fields:

- `n_cases`
- `repeat_consistency_rate`
- `order_stability_rate`
- `noise_resistance_rate`
- `description_robustness_rate`

Each scorecard should include its own `n_cases` value so the aggregate metrics
are interpretable at a glance.

Each scorecard metric field should be computed as the mean of the corresponding
per-case metric across the applicable benchmark cases.

The suite should also retain raw per-case metric fields in the report so future
analysis does not depend only on aggregates.

## 10. Pass/Fail Policy

The evaluation layer should serve two purposes:

1. produce readable benchmark values
2. fail on material regressions

Version 0.1 should use conservative deterministic assertions:

- each curated case may assert exact selected filenames when the benchmark is
  meant to lock behavior
- aggregate suite tests should assert per-metric thresholds from the scorecard
  rather than a single frozen total score

This split avoids an overly brittle suite while still making regressions
actionable.

Recommended initial threshold style:

- `mean_f1_at_k >= floor`
- `top1_hit_rate >= floor`
- `full_match_rate >= floor`
- `repeat_consistency_rate == 1.0`
- `order_stability_rate == 1.0`
- `description_robustness_rate == 1.0` for neutral non-target rewrites
- `noise_resistance_rate >= floor`

`noise_resistance_rate` should not be forced to `1.0` universally. Adding
distractors can legitimately affect top-k membership, especially when
`MAX_RECALLED_MEMORIES` is saturated or when the perturbation is intentionally
competitive.

Accordingly:

1. default helper-generated noise for invariance checks should use clearly
   irrelevant distractors
2. noise thresholds should be case-class-specific and may be lower than `1.0`
3. competitive-noise scenarios should be treated as explicit benchmark cases,
   not hidden inside a blanket invariance assumption

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
- parse the `Available memories:` section from the assembled user message
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
3. write `index_content` to the case `MEMORY.md`
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
- expected filenames must be present in `index_content` / the generated
  `MEMORY.md`
- every filename referenced by `index_content` must have a corresponding key in
  `memory_files`
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

- what are the current quality metrics
- what are the current stability metrics
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
