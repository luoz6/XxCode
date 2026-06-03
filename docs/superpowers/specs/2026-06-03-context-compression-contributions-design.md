# Runtime Context Compression Contribution Testing Design

> Version 0.1 | 2026-06-03 | Scope: define deterministic contribution metrics and TDD coverage for L1-L4 runtime context compression

## 1. Objective

The runtime context pipeline already compresses conversation history across four
levels:

- L1 snip
- L2 microcompact
- L3 collapse
- L4 autocompact

What is still missing is a trustworthy way to measure how much each layer
actually contributes. The goal of this design is to add contribution-oriented
tests so future changes can answer:

- how many tokens each level freed
- how many characters or message units each level reduced
- whether L4 produced a real end-to-end reduction rather than merely firing

This work is about runtime message compression, not system-prompt attachment
budgeting.

## 2. Current State

The current implementation already exposes partial statistics through
`CompressionStats` in `src/xxcode/context/pipeline.py`.

Existing fields:

- `tokens_before`
- `tokens_after`
- `snip_removed`
- `micro_truncated`
- `collapse_count`
- `auto_triggered`

These fields are useful, but they are not yet sufficient to measure
contribution cleanly:

- `tokens_before` and `tokens_after` only show total effect
- `snip_removed` is character-based, not token-based
- `micro_truncated` is not yet a strong contribution metric by itself
- `collapse_count` does not directly express tokens saved
- `auto_triggered` shows activation, not reduction

As a result, the test suite cannot yet prove what each level contributed.

## 3. Problem To Solve

We need a deterministic testable contract for contribution measurement.

The contract should let us verify:

1. each layer can be triggered intentionally
2. each triggered layer reports a meaningful contribution
3. total compression remains internally consistent
4. L4 can be tested without live model calls

## 4. Design Goals

This design should:

1. keep contribution metrics deterministic
2. treat tokens as the primary compression signal
3. keep chars and message counts as secondary diagnostic signals
4. avoid live API or model dependencies
5. work naturally with TDD red-green-refactor

## 5. Non-Goals

This design does not:

- add live summarizer integration tests
- benchmark semantic quality of summaries
- change prompt-builder snapshot coverage
- replace the existing hardening tests

## 6. Contribution Metric Contract

### 6.1 Primary Metrics

`CompressionStats` should grow explicit per-level token contribution fields:

- `snip_tokens_freed`
- `micro_tokens_freed`
- `collapse_tokens_freed`
- `auto_tokens_freed`

These fields represent the approximate token reduction attributable to each
level as executed within the pipeline.

### 6.2 Secondary Metrics

Secondary fields remain useful and should be preserved or clarified:

- `snip_removed`: characters removed by L1
- `micro_truncated`: number of tool results actually cleared or truncated by L2
- `collapse_count`: number of messages or exchanges removed by L3, but its
  exact meaning must be made explicit in code and tests
- `auto_triggered`: whether L4 ran

### 6.3 Consistency Rule

The tests should enforce a simple consistency rule:

- `tokens_before >= tokens_after`
- each `*_tokens_freed` is non-negative
- when a layer materially changes the message list, its token contribution is
  expected to be positive

The sum of per-level token contributions does not need to be mathematically
perfect across every edge case, but it should be directionally and locally
correct for deterministic fixtures.

## 7. L4 Testing Policy

L4 should be tested deterministically by monkeypatching `_autocompact()` to
return a fixed summary string.

This is the chosen policy because it keeps the tests:

- offline
- deterministic
- fast
- focused on pipeline behavior rather than API client streaming details

L4 tests should verify:

- `auto_triggered` is `True`
- `auto_tokens_freed > 0` for an oversized conversation
- the injected summary message appears in the compressed result
- budget carryover still deducts the pre-compact waterline

## 8. Test Design

Add a focused test module:

- `tests/context/test_compression_contributions.py`

### 8.1 L1 Snip Scenario

Construct a message history containing noisy tool output such as:

- pip install progress
- repeated download lines
- progress bars

Assertions:

- L1 triggers before later layers can finish the job
- `snip_removed > 0`
- `snip_tokens_freed > 0`
- the resulting tool output is visibly smaller

### 8.2 L2 Microcompact Scenario

Construct several compressible `tool_result` blocks from eligible tools such as:

- `read_file`
- `run_shell`
- `grep_search`

Keep the most recent result fresh and make older ones stale.

Assertions:

- `micro_truncated` equals the number of stale compressible results actually
  cleared
- `micro_tokens_freed > 0`
- recent preserved results remain intact

### 8.3 L3 Collapse Scenario

Construct multiple older exchanges followed by a small recent tail so that L3
must fold older history.

Assertions:

- `collapse_tokens_freed > 0`
- `collapse_count > 0`
- a collapsed summary message appears
- recent exchanges are still preserved

### 8.4 L4 Autocompact Scenario

Construct a very large history that remains above threshold after L1-L3.
Monkeypatch `_autocompact()` to return a fixed summary.

Assertions:

- `auto_triggered is True`
- `auto_tokens_freed > 0`
- the result includes `[Conversation summary]`
- the old message history is replaced per the L4 contract
- task budget carryover deducts the pre-L4 token waterline

### 8.5 End-to-End Contribution Scenario

Construct one intentionally oversized mixed history that can exercise multiple
layers in sequence.

Assertions:

- `tokens_before > tokens_after`
- at least two per-level contribution fields are positive
- the combined result is smaller and structurally valid

## 9. TDD Execution Order

Implementation should follow red-green-refactor.

### Step 1

Write failing tests for the new `CompressionStats` token contribution fields and
L1 contribution behavior.

### Step 2

Implement the minimum L1 accounting needed to pass.

### Step 3

Write failing L2 contribution tests, then implement the minimum accounting
needed to pass.

### Step 4

Write failing L3 contribution tests, then implement the minimum accounting
needed to pass.

### Step 5

Write failing deterministic L4 tests, then implement the minimum accounting
and any summary-injection clarifications needed to pass.

### Step 6

Add the mixed end-to-end regression after individual layers are green.

## 10. Risks And Mitigations

### Risk 1: Stat Semantics Stay Ambiguous

If existing fields retain fuzzy meanings, tests may pass while the numbers are
misleading.

Mitigation:

- define each field's meaning explicitly in assertions
- prefer new token contribution fields over overloading existing ones

### Risk 2: Fixtures Accidentally Trigger The Wrong Layer

If one fixture is too large or too small, the pipeline may bypass the intended
layer.

Mitigation:

- tune `current_tokens`, thresholds, and fixture shape carefully
- keep per-layer test messages intentionally narrow

### Risk 3: L4 Becomes Flaky

If L4 depends on streaming or remote state, the suite will flap.

Mitigation:

- monkeypatch `_autocompact()` directly
- avoid client-level fake stream tests in this phase

## 11. Acceptance Criteria

This design is complete when:

1. the runtime compression pipeline reports explicit per-level token
   contributions
2. tests cover L1 through L4 deterministically
3. total `tokens_before` and `tokens_after` reductions are validated
4. L4 coverage requires no live API dependency
5. the new tests are written and implemented in TDD order

## 12. Spec Self-Review

Checklist results:

- Placeholder scan: no `TODO`, `TBD`, or unresolved blanks remain.
- Internal consistency: tokens are primary metrics, chars and message counts are
  secondary diagnostics, and L4 remains deterministic.
- Scope check: this spec stays focused on runtime message compression
  contribution testing.
- Ambiguity check: L4 explicitly uses a monkeypatched summarizer rather than a
  fake streamed client.
