# Deterministic Context Engineering Evaluation Design

> Version 0.1 | 2026-06-03 | Scope: define an SDD-first, TDD-implemented system-level evaluation framework for context snapshots produced by the runtime context pipeline

## 1. Objective

The repository now has two useful but separate context-adjacent evaluation
directions:

- memory recall quality and stability evaluation
- runtime compression contribution evaluation

What it still does not have is a system-level way to answer this product
question:

- given a fixed multi-turn scenario, does the full context engineering stack
  prepare the right final context snapshot for the model

Version 0.1 adds a deterministic evaluation layer for the assembled context
snapshot only. The first version measures:

- required context preservation
- noise exclusion
- ordering and structural stability
- budget compliance

It does not measure final model answer quality in this phase.

## 2. Current State

Relevant existing areas:

- `src/xxcode/context/pipeline.py`
- `tests/context/test_compression_contributions.py`
- `tests/context/test_prompt_builder.py`
- `tests/memory/test_recall_eval.py`
- `tests/memory/test_recall_stability.py`
- `tests/memory/helpers/recall_eval.py`

Current coverage already proves useful local properties:

- memory recall can be benchmarked deterministically
- compression layers can report deterministic contribution metrics
- prompt-building contracts can be asserted

What is still missing is a system-level checkpoint that evaluates the final
context prepared for a future model call after recall, compression, and
assembly decisions have interacted.

## 3. Problem To Solve

The context system has multiple specialized parts:

- recall decides which long-term memories enter the snapshot
- compression decides what conversational history is reduced or removed
- assembly decides how the final prompt-like context is ordered and budgeted

Each of those can pass its own local tests while the combined result is still
wrong for the actual task. Typical failure modes include:

- a critical user constraint disappears after long history compression
- the right memory exists but never reaches the final snapshot
- noisy tool output remains while more important recent context is lost
- section ordering changes make the snapshot harder to reason about
- the final context exceeds budget even though individual components looked fine

The missing piece is a deterministic system-level evaluation that checks the
final snapshot directly without depending on live model calls.

## 4. Design Goals

This design should:

1. stay fully deterministic and CI-safe
2. evaluate the system-level context snapshot rather than a single submodule
3. reuse real runtime paths wherever deterministic seams already exist
4. keep memory recall and compression scorecards separate from the new system
   scorecard
5. use stable structure-aware assertions that fit TDD red-green-refactor
6. remain small enough for a focused first implementation phase

## 5. Non-Goals

This design does not:

- replace memory recall evaluation
- replace compression contribution evaluation
- grade final assistant answer quality
- benchmark live LLM semantic performance
- measure extraction quality or long-horizon memory consistency
- create a single blended score across memory, compression, and system context

## 6. Evaluation Scope And Boundary

Version 0.1 evaluates one thing only:

- whether the final context snapshot prepared for a target turn is correct,
  complete enough, clean enough, and within budget

This is a system-level context engineering evaluation, but it is not an
end-to-end answer-generation evaluation.

The intended layer boundaries are:

- memory recall evaluation answers: was the memory recall stage good
- compression evaluation answers: did the compression pipeline behave correctly
- context engineering evaluation answers: after those layers interact, is the
  final prepared snapshot fit for the task

These scorecards must remain separate.

## 7. Chosen Evaluation Strategy

Version 0.1 uses deterministic multi-turn replay with structured snapshot
assertions.

The framework should:

1. materialize a fixed memory directory
2. replay a fixed conversation history up to a target turn
3. run the real deterministic context-preparation path
4. capture the final context snapshot
5. assert required semantic and structural properties
6. aggregate case results into a system-level scorecard

The first version intentionally stops at the snapshot boundary rather than
asking a responder model to generate a final answer.

## 8. Deterministic Pipeline Contract

### 8.1 Deterministic Recall

Recall must use a deterministic selector client rather than a live LLM. Version
0.1 should reuse the same general approach already established in
`tests/memory/helpers/recall_eval.py`.

This means the evaluation continues to exercise the real recall pipeline while
keeping the selection result offline and repeatable.

### 8.2 Deterministic Compression

Compression must also remain deterministic for all system evaluation cases.

Version 0.1 treats the following paths as deterministic:

- L1 snip
- L2 microcompact
- L3 collapse

L4 autocompact must never call a live summarizer in this evaluation suite.

### 8.3 L4 Policy For Phase One

Phase one uses this policy:

- the core system-level suite disables L4 by default
- a small number of explicit L4 system cases may be added separately
- any such L4 case must use a fixed summary stub

This keeps the main suite stable while still allowing targeted system-level
coverage for summary injection behavior.

### 8.4 Per-Case Determinism Declaration

Each evaluation case should explicitly declare whether:

- recall is expected to activate
- compression is expected to activate
- L4 is disallowed
- L4 is allowed and replaced with a fixed stub

No case should rely on implicit non-deterministic behavior.

## 9. Assertion Layering Policy

The framework must separate semantic assertions from structure-sensitive
assertions.

### 9.1 Semantic Assertions

Semantic assertions check meaning-bearing properties of the final snapshot, for
example:

- required text fragments are present
- forbidden text fragments are absent
- recent task-critical context is preserved
- the final snapshot is within budget

These assertions should live in:

- `tests/context/test_context_engineering_eval.py`

### 9.2 Structural And Stability Assertions

Structure-sensitive assertions check ordering and perturbation stability, for
example:

- section order
- summary placement
- ordering stability under replay perturbations
- structural behavior under irrelevant noise

These assertions should live in:

- `tests/context/test_context_engineering_stability.py`

This split reduces brittleness. A section reordering should not automatically
invalidate semantic correctness tests.

## 10. Case Contract

Each replay case should define a stable data contract. Version 0.1 recommends:

- `case_id`
- `scenario`
- `messages`
- `memory_index_content`
- `memory_files`
- `context_limit`
- `threshold`
- `target_turn_index`
- `expected_present`
- `expected_absent`
- `expected_order`
- `required_sections`
- `expected_diagnostics`
- `budget_expectation`

Field meanings:

- `messages`: fixed conversation history including tool-related content if
  needed
- `memory_index_content`: literal `MEMORY.md` content for the case
- `memory_files`: filename to markdown content mapping
- `target_turn_index`: replay cutoff used to decide which prefix becomes the
  evaluated snapshot
- `expected_present`: phrases or anchors that must appear
- `expected_absent`: phrases or anchors that must not appear
- `expected_order`: relative ordering constraints between important anchors
- `required_sections`: named sections that must exist if the snapshot
  representation exposes them
- `expected_diagnostics`: booleans or exact values for activation flags such as
  recall used, compression used, summary injected
- `budget_expectation`: constraints such as final token count less than the
  case soft limit

## 11. Snapshot Representation Contract

The evaluation helper may expose both:

- a flattened text snapshot
- a structured snapshot view

The flattened text snapshot is the primary assertion surface for semantic
checks.

The structured snapshot view is optional and should only be used for targeted
ordering or section existence assertions. Version 0.1 should avoid overfitting
tests to fragile internal structure indices.

In particular:

- semantic tests should prefer text anchors
- structure tests may use named sections
- tests should not encode assumptions like "this content must be at section
  index 3" unless that index is part of an explicit contract

## 12. Metric Contract

Version 0.1 should expose a system-level scorecard rather than a blended total
score.

### 12.1 Completeness Metrics

Recommended fields:

- `n_cases`
- `required_content_hit_rate`
- `required_order_pass_rate`
- `section_presence_rate`
- `recent_context_preservation_rate`

### 12.2 Cleanliness Metrics

Recommended fields:

- `noise_exclusion_rate`
- `stale_content_exclusion_rate`
- `forbidden_content_absence_rate`

### 12.3 Budget And Control Metrics

Recommended fields:

- `budget_pass_rate`
- `compression_activation_rate`
- `recall_activation_rate`
- `snapshot_validity_rate`

The framework should also preserve raw per-case outcomes so failures remain easy
to diagnose from pytest output.

## 13. Pass/Fail Policy

The system evaluation should serve two purposes:

1. provide readable scorecard values
2. fail on material regressions

Version 0.1 should use:

- exact structured assertions for curated per-case expectations
- aggregate threshold assertions for the scorecard

Recommended threshold style:

- `required_content_hit_rate == 1.0`
- `forbidden_content_absence_rate == 1.0`
- `budget_pass_rate == 1.0`
- `snapshot_validity_rate == 1.0`
- `required_order_pass_rate >= floor`
- `noise_exclusion_rate >= floor`
- `compression_activation_rate >= floor` only for cases that explicitly expect
  compression
- `recall_activation_rate >= floor` only for cases that explicitly expect
  recall

Exact floors should be finalized during TDD once the curated corpus is in
place.

## 14. Component Design

Add:

- `tests/context/test_context_engineering_eval.py`
- `tests/context/test_context_engineering_stability.py`
- `tests/context/helpers/context_eval.py`

Responsibilities for `context_eval.py`:

- define case dataclasses
- materialize memory fixtures
- slice replay history to the target turn
- run deterministic recall and deterministic compression
- capture final snapshot text and diagnostics
- evaluate semantic assertions
- evaluate structural assertions
- compute per-case metrics
- compute aggregate scorecards
- render compact summaries for pytest output

Evaluation-specific score objects should remain under `tests/` in phase one.

## 15. Data Flow

For each case:

1. materialize a temporary memory directory
2. write `MEMORY.md`
3. write memory files
4. build the replay history prefix ending at `target_turn_index`
5. run the deterministic context-preparation path
6. capture the final snapshot text, optional section view, tokens, and
   diagnostics
7. apply semantic assertions
8. apply structural assertions if the case belongs to the stability suite
9. record per-case outcomes
10. aggregate scorecard values

For perturbation-based stability cases:

1. run the baseline case
2. apply one controlled perturbation
3. rerun the same target turn
4. compare the resulting snapshot against the intended stability contract

## 16. Initial Case Categories

Version 0.1 should start with a small curated suite. Recommended categories:

1. early user constraint survives long history
2. relevant memory is included in the final snapshot
3. irrelevant noise is excluded while recent context is preserved
4. snapshot stays within budget under forced compression
5. ordering remains stable under benign replay perturbations
6. optional targeted L4 case with fixed summary stub

The initial suite should stay intentionally small. Signal clarity matters more
than corpus size in phase one.

## 17. Concrete End-To-End Example

This section provides a concrete case to anchor the contract.

### 17.1 Memory Inputs

`MEMORY.md`

```text
- [indexed] python-style.md: User prefers pandas and pathlib over manual csv parsing
- [indexed] release-risk.md: Do not modify settings.py during release freeze
```

`python-style.md`

```markdown
---
metadata:
  type: user
---

Use pandas for dataframe-style analysis and pathlib for file paths.
```

`release-risk.md`

```markdown
---
metadata:
  type: project
---

Release freeze is active. Do not modify settings.py without explicit approval.
```

### 17.2 Replay History

Example message history:

1. user asks for help updating data-processing logic
2. assistant proposes inspecting files
3. tool output contains long noisy shell logs
4. user says "do not change settings.py"
5. more tool results arrive
6. user asks for a final plan for `processor.py`

The target turn is the last user request.

### 17.3 Expected Snapshot Assertions

Semantic expectations:

- final snapshot contains `do not change settings.py`
- final snapshot contains a pandas-related memory anchor
- final snapshot does not contain raw repeated shell download noise
- final snapshot preserves the most recent user request about `processor.py`

Budget expectations:

- final token count is below the case soft limit

Diagnostic expectations:

- recall activated
- compression activated
- L4 not used

Structural expectations:

- recalled memory content appears before the newest task request only if that is
  the explicit runtime contract, otherwise only named section ordering should be
  asserted
- recent conversation content remains after any summary or collapse block

This example is not the only valid case shape, but it makes the intended
contract concrete.

## 18. TDD Execution Order

Implementation must follow red-green-refactor.

Recommended order:

1. write the smallest failing semantic case asserting one required constraint is
   present in the final snapshot
2. implement the minimum helper needed to materialize a case and capture a
   flattened snapshot
3. add a failing memory-inclusion case
4. add reusable semantic assertion helpers such as `contains_all` and
   `contains_none`
5. add a failing budget case
6. add scorecard aggregation once several per-case outcomes exist
7. add structure-sensitive stability tests only after the semantic core is
   green
8. add an optional targeted L4 case with a fixed summary stub last

The goal is to avoid building a large abstract evaluation framework before the
first real red-green loop succeeds.

## 19. Risks And Mitigations

### Risk 1: Hidden Non-Determinism In The Pipeline

If any replay path still depends on live model behavior, the suite will flap.

Mitigation:

- make deterministic seams explicit in the helper
- disable L4 by default
- require fixed stubs for any allowed L4 case

### Risk 2: Snapshot Structure Becomes Brittle

If tests overfit section indexes or internal layouts, harmless refactors will
cause noisy failures.

Mitigation:

- keep semantic tests text-anchor based
- isolate structure-sensitive assertions into the stability suite
- use named sections rather than raw indices when possible

### Risk 3: Scope Drifts Into Final Answer Evaluation

If phase one starts checking generated answers, determinism and failure
diagnostics become much harder.

Mitigation:

- keep the evaluation boundary at the final context snapshot
- defer answer-quality testing to a later phase

## 20. Acceptance Criteria

This design is complete when:

1. the repository gains a deterministic system-level context snapshot
   evaluation suite
2. the new scorecard remains distinct from memory recall and compression
   scorecards
3. semantic and structural assertions are separated into different test modules
4. the suite runs without live LLM calls
5. the initial curated cases exercise recall, compression, and assembly
   interaction
6. the suite reports concrete system-level snapshot metrics
7. implementation can proceed in clear TDD red-green-refactor steps

## 21. Spec Self-Review

Checklist results:

- Placeholder scan: no `TODO`, `TBD`, or unresolved blanks remain.
- Internal consistency: the design stays at the system snapshot boundary, keeps
  memory and compression scorecards separate, and explicitly constrains
  determinism.
- Scope check: phase one is intentionally limited to deterministic snapshot
  evaluation rather than final answer grading.
- Ambiguity check: the L4 policy, assertion layering, case contract, and
  concrete example all make the intended boundaries explicit.
