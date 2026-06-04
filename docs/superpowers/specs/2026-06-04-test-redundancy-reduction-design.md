# Test Redundancy Reduction Design

> Version 0.1 | 2026-06-04 | Scope: analyze the repository test suite, identify redundant patterns, and define a low-risk refactor plan that removes duplication inside individual test files only

## 1. Objective

The repository has grown a large and diverse automated test suite under
`tests/`. Coverage is broad, but several areas now repeat the same setup,
helper, and orchestration patterns many times inside the same file.

The goal of this work is to:

- analyze the full test suite for redundancy
- prioritize the highest-value duplicate patterns
- remove redundant code from tests with low behavioral risk
- preserve test intent, failure clarity, and current runtime behavior

This phase is about test maintenance quality, not production feature changes.

## 2. User Request And Constraints

The user asked for:

- analysis of the codebase with emphasis on test files
- analysis of all test files
- removal of redundant parts from the tests

The user also approved a strict low-risk boundary:

- only perform test-side deduplication
- do not reorganize shared test infrastructure
- do not refactor production code as part of this work

This design therefore assumes:

- no new shared cross-file helper modules under `tests/`
- no `tests/conftest.py` restructuring in this phase
- no production-code changes unless an unavoidable blocker is discovered

## 3. Current State

The current repository contains a substantial test surface across these areas:

- `tests/memory/`
- `tests/context/`
- `tests/agent/`
- `tests/tools/`
- `tests/mcp/`
- top-level runtime and integration tests under `tests/`

Initial scanning shows three important realities:

1. the suite is broad enough that not every long file is truly redundant
2. the heaviest duplication is usually test data setup rather than assertions
3. the most valuable cleanup targets are concentrated in a small number of
   files

Examples of observed repetition patterns:

- repeated `Config(...)` construction in agent/runtime tests
- repeated `SimpleNamespace(...)` test config construction in memory tests
- repeated `SubAgent(...)` construction with identical defaults
- repeated async wrapper patterns such as `async def _run(): ...` followed by
  `asyncio.run(_run())`
- repeated benchmark loops that build metrics and scorecards with only minor
  per-file variation
- repeated evaluation-case construction where only a few literals differ

## 4. Scan Summary

The full test suite was scanned to understand where redundancy is concentrated.
The scan focused on:

- all `test_*.py` files under `tests/`
- file size and test count
- repeated code windows
- repeated async execution patterns
- repeated scorecard and case-run loops

The strongest hotspots found during the scan were:

- `tests/memory/test_agent_memory.py`
- `tests/agent/test_loop_hardening.py`
- `tests/test_permission_flow.py`
- `tests/test_task_runtime.py`
- `tests/memory/test_recall_eval.py`
- `tests/memory/test_extraction_eval.py`
- `tests/context/test_context_engineering_eval.py`
- `tests/memory/test_recall_stability.py`

Important supporting observations:

- many files are large because they cover many distinct behaviors, not because
  they are badly duplicated
- some smaller evaluation files have very high structural repetition and are
  better cleanup candidates than longer integration files
- several files already contain local helpers; the problem is inconsistent
  helper extraction rather than total absence of structure

## 5. Problem To Solve

Redundant test code creates several maintenance problems:

- setup changes require editing the same structure in many places
- tests become harder to read because scenario intent is buried under repeated
  scaffolding
- repetitive async wrappers make files longer without adding signal
- duplicated builders increase the chance of subtle drift between near-identical
  tests
- evaluation-style suites become harder to extend consistently

At the same time, test refactoring has its own risk:

- over-abstracted tests hide behavior behind helpers
- aggressive parameterization can make failures harder to interpret
- cross-file helper extraction can widen blast radius

The design must reduce duplication without making the suite more opaque.

## 6. Design Goals

This work should:

1. keep test behavior unchanged
2. reduce obvious redundancy where setup and control flow dominate the file
3. preserve scenario readability and failure diagnostics
4. stay within test files rather than reshaping shared infrastructure
5. focus effort on the highest-value hotspots first
6. leave distinct tests distinct when their intent is meaningfully different

## 7. Non-Goals

This design does not:

- change production behavior
- move helper logic into shared infrastructure such as `tests/conftest.py`
- introduce new cross-file helper modules
- rewrite the whole test suite into a common DSL
- optimize for minimum line count at the expense of clarity
- convert every repeated pattern into parameterization

## 8. Candidate Approaches

Three approaches were considered.

### 8.1 File-Local Deduplication

Refactor redundancy inside each target file only by introducing local builders,
local fixtures, local helper functions, and selective parameterization.

Benefits:

- lowest risk
- easy to review
- failures stay close to the file that defines the behavior
- no shared-infrastructure churn

Trade-offs:

- similar helpers may still exist in multiple files
- total line reduction is moderate rather than maximal

### 8.2 Subdomain-Local Shared Helpers

Create small shared helper layers inside areas such as `tests/memory/`.

Benefits:

- stronger deduplication
- better consistency across related files

Trade-offs:

- starts to blur into test infrastructure refactoring
- raises the blast radius of mistakes
- conflicts with the approved low-risk boundary

### 8.3 Broad Parameterization Rewrite

Convert many repetitive tests into table-driven forms.

Benefits:

- highest line-count reduction
- uniform structure

Trade-offs:

- risks weaker readability
- risks poorer failure messages
- easy to over-abstract unlike-for-like tests

## 9. Chosen Strategy

This work adopts approach `8.1 File-Local Deduplication`.

Why this is the best fit:

- it aligns with the approved low-risk scope
- it targets the actual redundancy hotspots without widening scope
- it keeps test intent visible in the same file as the behavior under test
- it avoids structural churn in shared test plumbing

The core principle is:

- remove repeated scaffolding, not meaningful scenario detail

## 10. File Selection Policy

All test files are part of the analysis scope, but only a subset should be
edited in this phase.

### 10.1 Files To Prioritize For Refactor

- `tests/memory/test_agent_memory.py`
- `tests/agent/test_loop_hardening.py`
- `tests/test_permission_flow.py`
- `tests/test_task_runtime.py`
- `tests/memory/test_recall_eval.py`
- `tests/memory/test_recall_stability.py`
- `tests/memory/test_extraction_eval.py`
- `tests/context/test_context_engineering_eval.py`

These files were chosen because the repeated setup cost is high and the
patterns are stable enough to refactor safely.

### 10.2 Files To Analyze But Usually Leave Alone

Examples include:

- short files with only a few tests
- files where most lines are assertion-specific rather than setup-specific
- files with many distinct branches that only look similar superficially

In those files, the output of this phase may be:

- documented redundancy observations
- no code change

That is acceptable and preferred over forced abstraction.

## 11. Redundancy Categories And Planned Refactors

### 11.1 Repeated Case Construction In Evaluation Tests

Target files:

- `tests/memory/test_recall_eval.py`
- `tests/memory/test_extraction_eval.py`
- `tests/context/test_context_engineering_eval.py`

Observed pattern:

- repeated construction of dataclass-style evaluation cases
- repeated assertions over metrics or scorecards
- repeated loops over benchmark cases

Planned refactor:

- introduce file-local case factory helpers for common defaults
- selectively use `pytest.mark.parametrize` only when test structure is
  identical and only the data changes
- keep named test functions for behaviorally important scenarios

Explicit limit:

- do not convert nuanced scenario tests into opaque data tables

### 11.2 Repeated Async Wrapper Boilerplate

Target files:

- `tests/memory/test_recall_eval.py`
- `tests/test_permission_flow.py`
- `tests/test_task_runtime.py`
- `tests/agent/test_loop_hardening.py`

Observed pattern:

- local `async def _run()` wrappers used once
- immediate `asyncio.run(_run())` execution
- repeated event collection logic

Planned refactor:

- replace one-off async wrappers with direct `@pytest.mark.asyncio` tests where
  it improves clarity
- introduce file-local async helpers when the orchestration is repeated across
  multiple tests
- keep helper names concrete, such as `_run_until_done` or `_collect_events`

Explicit limit:

- do not mix sync and async styles in a way that makes files harder to follow

### 11.3 Repeated Object Assembly In Long Integration Tests

Target files:

- `tests/memory/test_agent_memory.py`
- `tests/agent/test_loop_hardening.py`
- `tests/test_permission_flow.py`
- `tests/test_task_runtime.py`

Observed pattern:

- repeated construction of `Config(...)`
- repeated `SimpleNamespace(...)` config setup
- repeated `definition = SimpleNamespace(...)`
- repeated `SubAgent(...)` construction
- repeated `AgentState(...)` construction with only minor variations

Planned refactor:

- add file-local builders with clear defaults
- keep overrides explicit through keyword-only helper arguments
- preserve important scenario-specific differences inline in each test

Illustrative helper shapes:

```python
def _make_config(tmp_path: Path, **overrides) -> Config: ...


def _make_subagent(tmp_path: Path, **overrides) -> SubAgent: ...


def _make_state(text: str, **overrides) -> AgentState: ...
```

Explicit limit:

- do not hide core scenario facts inside a builder if that would weaken test
  readability

### 11.4 Repeated Scorecard Flow

Target files:

- `tests/memory/test_recall_eval.py`
- `tests/memory/test_recall_stability.py`
- `tests/memory/test_extraction_eval.py`
- `tests/context/test_context_engineering_eval.py`

Observed pattern:

- iterate benchmark cases
- compute per-case metrics
- build a scorecard
- assert summary fields or thresholds

Planned refactor:

- extract only the repeated control flow
- keep per-file thresholds and domain-specific assertions local

Explicit limit:

- do not build a shared generic scorecard assertion helper across files

## 12. Local Helper Design Rules

All helpers introduced in this phase should follow these rules:

1. stay inside the file they serve
2. encode only genuinely repeated structure
3. use explicit names tied to domain behavior
4. prefer keyword overrides over positional mystery arguments
5. keep scenario-specific assertions in the test body

Warning signs that a helper is too abstract:

- the helper name no longer reveals what it builds
- readers must jump around the file to understand a simple test
- test data is passed as large nested dictionaries just to satisfy the helper

If any of those appear, the refactor should be scaled back.

## 13. Readability Policy

Reducing redundancy is not the only goal. The tests must remain easy to read.

This phase therefore prefers:

- repeated literal assertions over clever generic assertion engines
- a small number of concrete builders over a generalized framework
- behavior-named tests over anonymous parameter tables when the scenario matters

Parameterization is appropriate only when all of the following are true:

- control flow is the same
- assertion shape is the same
- case names remain descriptive
- failure output will still identify the scenario clearly

## 14. Verification Plan

Verification is split into three layers.

### 14.1 File-Level Regression

For every edited file:

- run that file directly under pytest
- confirm that the refactor did not change pass/fail behavior

### 14.2 Subdomain Spot Checks

For touched areas such as `memory`, `agent`, and `context`:

- run neighboring tests that rely on similar patterns
- check for accidental coupling to file-local helper extraction

### 14.3 Diff Review

Review each edited file specifically for:

- helper over-abstraction
- hidden scenario details
- worse failure readability after parameterization
- accidental introduction of shared assumptions

## 15. Implementation Order

Recommended execution order:

1. refactor the smallest evaluation-style files first
2. refactor async wrapper duplication next
3. refactor long setup-heavy integration files after the local patterns are
   validated
4. run file-level and subdomain-level regression checks after each cluster

Recommended cluster order:

1. `tests/memory/test_recall_eval.py`
2. `tests/memory/test_recall_stability.py`
3. `tests/memory/test_extraction_eval.py`
4. `tests/context/test_context_engineering_eval.py`
5. `tests/agent/test_loop_hardening.py`
6. `tests/test_permission_flow.py`
7. `tests/test_task_runtime.py`
8. `tests/memory/test_agent_memory.py`

This order starts with high-signal, lower-risk cleanup before touching the
largest integration-oriented test file.

## 16. Risks And Mitigations

### Risk 1: Helper Extraction Hides Test Intent

Mitigation:

- keep helpers file-local
- keep scenario-critical data inline
- prefer multiple small helpers over one generic builder

### Risk 2: Parameterization Weakens Failure Diagnostics

Mitigation:

- parameterize only structurally identical cases
- use descriptive case ids
- keep complex scenarios as standalone tests

### Risk 3: Cleanup Scope Expands Into Infrastructure Refactor

Mitigation:

- do not touch `tests/conftest.py` in this phase
- do not create new shared helper modules
- reject refactors whose value depends on cross-file sharing

### Risk 4: Long Files Contain Superficial Rather Than Real Duplication

Mitigation:

- require stable repeated shape before extracting helpers
- allow a file to remain unchanged after analysis if abstraction would be forced

## 17. Acceptance Criteria

This design is complete when:

1. all test files have been analyzed for redundancy
2. the highest-value redundant patterns are identified clearly
3. implementation stays within test files and avoids shared-infrastructure
   refactors
4. target files receive lower-boilerplate structure without changing behavior
5. edited tests remain at least as readable as before
6. verification covers every changed file and selected neighboring suites
7. unchanged files are left unchanged when deduplication would be forced or
   risky

## 18. Deliverables

The implementation that follows this design should produce:

- a concrete set of edited test files with reduced redundancy
- a short summary of which files were analyzed versus actually changed
- regression evidence from pytest runs for the affected areas
- a note of any intentionally deferred redundancy that was not safe to remove

## 19. Spec Self-Review

Checklist results:

- Placeholder scan: no `TODO`, `TBD`, or unresolved sections remain.
- Internal consistency: the document consistently enforces a test-only,
  file-local, low-risk deduplication scope.
- Scope check: the work remains bounded to analysis plus safe cleanup of target
  test files rather than broader infrastructure or production refactors.
- Ambiguity check: target files, allowed techniques, blocked techniques,
  verification layers, and implementation order are all explicit.
