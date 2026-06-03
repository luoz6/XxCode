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
- `micro_cleared`: exact number of compressible stale `tool_result` blocks
  replaced with the L2 placeholder
- `micro_truncated`: a legacy or diagnostic field only if still needed, but it
  should not be the exactness target of contribution tests
- `collapse_count`: net message reduction produced by `collapse_messages()`,
  not "collapsed exchange count"
- `auto_triggered`: whether L4 ran

### 6.3 Consistency Rule

The tests should enforce a simple consistency rule:

- `tokens_before >= tokens_after`
- each `*_tokens_freed` is non-negative
- when a layer materially changes the message list, its token contribution is
  expected to be positive
- when the fixture produces a positive overall reduction, the per-level token
  contributions should add up exactly to the total reduction because the
  definitions telescope across shared intermediate estimates

For fixtures with positive total reduction:

`sum(per_level_tokens_freed) == (tokens_before - tokens_after)`

For no-op or identity fixtures:

- `tokens_before == tokens_after`
- every per-level token contribution in scope is exactly `0`

This equality is expected to hold because adjacent contribution terms share the
same intermediate `token_count_with_estimation()` outputs:

- `snip_tokens_freed = tokens_before - post_L1_estimate`
- `micro_tokens_freed = post_L1_estimate - post_L2_estimate`
- `collapse_tokens_freed = post_L2_estimate - post_L3_estimate`
- `auto_tokens_freed = post_L3_estimate - tokens_after`

The intermediate terms cancel exactly, so the sum should equal
`tokens_before - tokens_after` rather than merely approximate it.

### 6.4 L1 Measurement Contract

L1 must distinguish between token-level and character-level measurement:

1. `snip_tokens_freed` is computed from the pipeline-visible token estimate
   before and after `snip_messages()`
2. `snip_removed` remains a character-only diagnostic signal derived from
   `tool_result` payload content
3. tests treat `snip_tokens_freed` as the primary assertion target and
   `snip_removed` as a supporting consistency signal only

This distinction is important because character savings are calculated only from
`tool_result` payload strings, while token estimation observes the full message
structure.

### 6.5 L2 Exactness Contract

L2 needs one exact count metric in addition to token contribution:

- `micro_cleared` must count only the stale compressible `tool_result` blocks
  actually rewritten to the placeholder

This avoids overloading `micro_truncated` with an exact meaning it does not
currently carry.

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
- budget carryover still deducts the pre-compact waterline when
  `state.task_budget_remaining` is explicitly initialized

L4 suppression tests should also verify:

- when `should_autocompact(...)` returns `False`, `auto_triggered` remains
  `False`
- `auto_tokens_freed == 0`
- the pipeline returns the post-L3 message list unchanged

## 8. Test Design

Add a focused test module:

- `tests/context/test_compression_contributions.py`

### 8.1 L1 Snip Scenario

This scenario explicitly tests `snip_messages()`, which is the L1 path used by
the current runtime pipeline. It does not treat `snip_compact_if_needed()` as
the same path.

Construct a message history containing noisy tool output such as:

- pip install progress
- repeated download lines
- progress bars

Token estimation strategy:

- use deterministic rough-estimate fixtures by controlling message character
  counts and omitting usage anchors

Assertions:

- L1 triggers before later layers can finish the job
- `snip_removed > 0`
- `snip_tokens_freed > 0`
- the resulting tool output is visibly smaller

Also add a no-op subcase:

- clean tool output with no matching noise patterns
- `snip_messages()` returns effectively identical payloads
- `snip_tokens_freed == 0`
- the pipeline can continue into later levels if still over threshold

### 8.1A Optional Non-Pipeline L1 Variant

If `snip_compact_if_needed()` is tested, it should be described separately as an
independent unit test because it is not currently wired into the runtime
pipeline path.

### 8.2 L2 Microcompact Scenario

Construct several compressible `tool_result` blocks from eligible tools such as:

- `read_file`
- `run_shell`
- `grep_search`

Keep the most recent result fresh and make older ones stale.

Token estimation strategy:

- prefer deterministic rough-estimate fixtures with controlled payload sizes
- monkeypatch token estimation only if exact fixture sizing becomes too brittle

Assertions:

- `micro_cleared` equals the exact number of stale compressible results
  actually cleared
- `micro_tokens_freed > 0`
- `micro_truncated`, if retained, is treated as diagnostic only
- recent preserved results remain intact

### 8.3 L3 Collapse Scenario

Construct multiple older exchanges followed by a small recent tail so that L3
must fold older history.

Token estimation strategy:

- use deterministic rough-estimate fixtures unless a targeted monkeypatch is
  needed to isolate L3 from earlier levels

Assertions:

- `collapse_tokens_freed > 0`
- `collapse_count` is asserted as the net message reduction from the
  `collapse_messages()` replacement behavior
- a collapsed summary message appears
- recent exchanges are still preserved

For example, if the older portion contributes `60` messages and is replaced by
`1` collapsed summary message, the exact expectation is:

- `collapse_count == 59`

### 8.4 L4 Autocompact Scenario

Construct a very large history that remains above threshold after L1-L3.
Monkeypatch `_autocompact()` to return a fixed summary.

Token estimation strategy:

- prefer deterministic rough-estimate fixtures
- if needed, monkeypatch token estimation only to isolate the L4 transition, not
  to fake the final accounting result

Budget setup precondition:

- create an `AgentState`
- set `state.task_budget_remaining = 50_000`
- ensure the fixture still has more than `4` messages after L1-L3 so
  `_inject_summary(..., keep_recent=2)` actually inserts the summary message

L4 entry accounting precondition:

- capture the post-L3 token waterline immediately before `_autocompact()`
- use that captured value as the expected budget deduction baseline

Assertions:

- `auto_triggered is True`
- `auto_tokens_freed > 0`
- the result includes `[Conversation summary]`
- the old message history is replaced per the L4 contract
- `state.task_budget_remaining == 50_000 - post_L3_token_count`

If a test intentionally omits `task_budget_remaining`, it should explicitly
state that budget-carryover assertions are out of scope for that fixture.

Also add a suppression subcase:

- monkeypatch or otherwise force `should_autocompact(...)` to return `False`
- assert `auto_triggered is False`
- assert `auto_tokens_freed == 0`
- assert no summary message is injected

### 8.5 End-to-End Contribution Scenario

Construct one intentionally oversized mixed history that can exercise multiple
layers in sequence.

Token estimation strategy:

- use deterministic rough-estimate fixtures with controlled text sizes
- do not rely on live API usage anchors for the baseline contribution suite

Assertions:

- `tokens_before > tokens_after`
- at least two per-level contribution fields are positive
- the summed per-level token contributions equal the total token reduction
- the combined result is smaller and structurally valid

## 9. TDD Execution Order

Implementation should follow red-green-refactor.

### Step 1

Write failing tests for the new `CompressionStats` token contribution fields and
L1 contribution behavior.

These initial fixtures should explicitly declare whether they rely on:

- controlled `rough_estimate` behavior through exact character counts
- or monkeypatched token estimation for isolation

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
- document the token estimation strategy for each fixture

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
4. the summed per-level token contributions equal the total token reduction on
   positive-reduction fixtures
5. L2 exact block clearing is verified through `micro_cleared`
6. L4 success and suppression paths both define `auto_tokens_freed`
7. L4 coverage requires no live API dependency
8. the new tests are written and implemented in TDD order

## 12. Spec Self-Review

Checklist results:

- Placeholder scan: no `TODO`, `TBD`, or unresolved blanks remain.
- Internal consistency: tokens are primary metrics, chars and message counts are
  secondary diagnostics, `micro_cleared` is the exact L2 count metric, and L4
  remains deterministic.
- Scope check: this spec stays focused on runtime message compression
  contribution testing.
- Ambiguity check: the spec now distinguishes token and character accounting,
  separates pipeline L1 from the unused L1 variant, defines `collapse_count` as
  net message reduction, and explicitly states L4 entry and budget
  preconditions.
