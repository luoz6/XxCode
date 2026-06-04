# XXCODE.md Rename Design

> Version 0.1 | 2026-06-04 | Scope: rename the project-instruction file from `CLAUDE.md` to `XXCODE.md` while preserving low-risk runtime compatibility

## 1. Objective

The repository currently treats `CLAUDE.md` as the project-instruction file that
can be loaded into the main agent prompt.

The goal of this work is to:

- make `XXCODE.md` the new canonical project-instruction filename
- preserve compatibility with existing projects that still use `CLAUDE.md`
- align runtime behavior, prompt wording, tests, and active documentation
- update all runtime-injected prompt strings that still name `CLAUDE.md`
- avoid duplicate loading when both filenames exist in the same directory

This is a naming and compatibility migration. It is not a redesign of prompt
priority, budgeting, or sub-agent attachment policy.

## 2. User Request And Constraints

The user asked to rename `CLAUDE.md` to `XXCODE.md`.

During clarification, the user explicitly chose the compatibility path rather
than an immediate hard cutover. The approved direction is:

- `XXCODE.md` becomes the official name
- `CLAUDE.md` remains supported during a transition period
- user-visible wording should move to `XXCODE.md`

The migration should therefore:

- prefer `XXCODE.md` at runtime
- fall back to `CLAUDE.md` when the new file is absent
- update tests and prompt snapshots to reflect the new canonical name
- avoid blanket rewriting of historical design records where the old name is
  part of the original context

## 3. Current State

The current implementation uses `CLAUDE.md` as a first-class concept in several
places:

- runtime loading in `src/xxcode/context/builder.py`
- prompt attachment heading `Project Instructions (CLAUDE.md)`
- system-prompt policy text that names `CLAUDE.md`
- memory-section exclusion text in `src/xxcode/context/builder.py`
- memory-extraction fallback prompt text in `src/xxcode/memory/extraction.py`
- file-backed extraction prompt text in `src/xxcode/memory/prompts/extraction_system.md`
- tests that create `CLAUDE.md` fixtures and assert prompt wording
- eval helpers that construct `cwd_files={"CLAUDE.md": ...}` fixtures
- prompt snapshots that embed the old name
- design and guide documents that describe the current behavior

The runtime loader currently walks upward from the current working directory and
collects `CLAUDE.md` files by directory, concatenating directory blocks with the
separator `\n\n---\n\n`.

Path convention for this design:

- repository-relative paths such as `src/xxcode/context/builder.py` are relative
  to the git root at `F:\agent\XxCode\XxCode`
- from the outer workspace root `F:\agent\XxCode`, prepend `XxCode/`

The main behavior risks in the current state are:

- the file name is now project-branded incorrectly for this repository
- a partial rename would leave runtime and prompt wording inconsistent
- supporting both names naively could cause duplicate content from the same
  directory
- the memory-section and extraction prompts could drift if only some of the
  runtime-injected `CLAUDE.md` references are updated
- the loader API name could remain historically misleading even after behavior
  changes

## 4. Problem To Solve

We need a deterministic migration contract for project-instruction files.

The contract must answer:

1. which filename is canonical going forward
2. how the loader behaves when only the old name exists
3. how the loader behaves when both names exist in the same directory
4. what the loader function itself should be named
5. which runtime-injected strings should be updated together
6. which tests, snapshots, and documents should change now
7. which historical documents should remain untouched

## 5. Design Goals

This design should:

1. make `XXCODE.md` the single canonical name for new usage
2. keep existing `CLAUDE.md`-based projects working
3. avoid duplicate project-instruction content per directory
4. keep nearest-directory-first ordering unchanged
5. keep the migration easy to reason about in tests
6. update active user-visible wording to match the new canonical name

## 6. Non-Goals

This design does not:

- remove compatibility with `CLAUDE.md` immediately
- change prompt priority ordering
- change project-instruction budget sizes
- change the rule that sub-agents omit the full project-instruction attachment
  by default
- create a new root-level `XXCODE.md` for this repository as part of the rename
- rewrite all historical design documents for naming consistency alone
- introduce a merge model where both files in the same directory are combined

## 7. Candidate Approaches

Three approaches were considered.

### 7.1 Runtime Compatibility Only

Add runtime support for `XXCODE.md` with fallback to `CLAUDE.md`, but leave most
existing wording and tests unchanged.

Benefits:

- smallest code diff
- lowest immediate risk

Trade-offs:

- leaves user-visible naming inconsistent
- keeps test and prompt terminology drifting from runtime behavior

### 7.2 Runtime Compatibility Plus Active-Surface Rename

Make `XXCODE.md` canonical across runtime behavior, prompt wording, tests,
snapshots, and active implementation documents, while keeping runtime fallback
for `CLAUDE.md`.

Benefits:

- user-visible behavior becomes coherent
- compatibility is preserved without semantic ambiguity
- tests encode the intended migration contract directly

Trade-offs:

- broader change set than runtime-only compatibility
- requires careful document selection to avoid rewriting historical context

### 7.3 Long-Term Dual Naming

Treat both names as equally first-class and allow both to be loaded or combined
indefinitely.

Benefits:

- minimal migration pressure on old projects

Trade-offs:

- keeps the contract permanently ambiguous
- risks duplicate or conflicting content
- guarantees future cleanup work

## 8. Chosen Strategy

This work adopts approach `7.2 Runtime Compatibility Plus Active-Surface Rename`.

Why this is the best fit:

- it matches the user-approved compatibility strategy
- it gives the repository one clear canonical name going forward
- it avoids the confusion of runtime support that still presents old wording to
  users
- it preserves compatibility without carrying permanent dual-brand semantics

The core principle is:

- `XXCODE.md` is canonical, `CLAUDE.md` is transitional compatibility

## 9. Runtime Loading Contract

### 9.1 Canonical Filename

`XXCODE.md` is the official project-instruction filename for this repository.

### 9.2 Per-Directory Resolution Rule

When scanning a directory during upward traversal:

1. if `XXCODE.md` exists, load it
2. else if `CLAUDE.md` exists, load it
3. else load nothing from that directory

Only one project-instruction file may be loaded per directory.

This rule prevents duplicate content when both filenames coexist in the same
location during migration.

### 9.3 Traversal Order

The existing nearest-directory-first behavior remains unchanged:

- the current directory is checked first
- then each parent directory
- content from the nearest directory appears first in the concatenated result
- directory blocks remain separated by `\n\n---\n\n` so truncation can continue
  to preserve file boundaries via `preserve_separator`

### 9.4 Compatibility Examples

Example 1:

- current directory contains `XXCODE.md`
- parent directory contains `CLAUDE.md`
- result: current `XXCODE.md` content appears first, parent `CLAUDE.md` content
  appears later

Example 2:

- a directory contains both `XXCODE.md` and `CLAUDE.md`
- result: only `XXCODE.md` is loaded for that directory

Example 3:

- no `XXCODE.md` exists anywhere
- some ancestor directories still contain `CLAUDE.md`
- result: those `CLAUDE.md` files are still loaded

### 9.5 Loader API Naming

The loader function should be renamed from `load_claude_md(cwd)` to
`load_project_instructions(cwd)`.

`load_xxcode_md(cwd)` was considered and rejected because it would still bake a
filename into the API surface. The behavior being modeled is semantic project
instruction loading, not a hardcoded file-brand concept.

The migration rule is:

- rename the function to `load_project_instructions`
- update all direct callers to the new name
- update all test doubles, monkeypatch targets, helper variables, and examples
  in the same change set
- do not keep a production alias named `load_claude_md`

The no-alias rule is intentional. Leaving a stale historical alias behind would
make future code search and review harder, and it would allow active test code
to continue referencing the wrong concept.

## 10. Prompt And Prompt-Adjacent Wording Changes

The user-visible prompt wording should move to the new canonical name now.

This includes:

- project-instruction priority wording in the system prompt
- trust wording that explains the role of the project-instruction file
- the system-prompt template source in `assets/system-prompt.md`
- attachment headings such as `Project Instructions (XXCODE.md)`
- `_MEMORY_SECTION_TEMPLATE` text in `src/xxcode/context/builder.py`
- the fallback memory-extraction prompt text in `src/xxcode/memory/extraction.py`
- the file-backed extraction prompt in
  `src/xxcode/memory/prompts/extraction_system.md`
- prompt assertions and snapshots that check those strings

The intent is that users interacting with the system see only the new official
name unless the repository is explicitly discussing backward compatibility.

The memory-section template and the two extraction prompts form one consistency
chain. They must be updated together so the main agent and the background memory
extraction path receive the same instruction about what content should not be
stored as memory.

## 11. Test Strategy

Tests should encode the migration contract rather than merely chase string
replacement.

### 11.1 Required Runtime Cases

The test suite should cover:

- only `XXCODE.md` exists
- only `CLAUDE.md` exists
- both names exist in the same directory and `XXCODE.md` wins
- multiple ancestor directories exist and nearest-directory-first ordering is
  preserved

### 11.2 Canonical Fixture Migration

Existing tests that exercise the canonical path should migrate their fixtures
from `CLAUDE.md` to `XXCODE.md`.

This includes, at minimum:

- `tests/context/test_prompt_builder.py`
- `tests/context/helpers/context_eval.py`
- `tests/context/test_context_engineering_eval.py`

The fixture policy is:

- canonical-path tests use `XXCODE.md`
- dedicated compatibility tests use `CLAUDE.md`
- dedicated precedence tests create both files in the same directory

The eval infrastructure should follow the same rule. Its current
`cwd_files={"CLAUDE.md": ...}` fixtures should move to `XXCODE.md` for baseline
cases, with separate fallback coverage added explicitly rather than relying on
old fixtures everywhere.

### 11.3 Prompt Contract Cases

The test suite should also confirm:

- prompt output uses `XXCODE.md` in canonical wording
- project-instruction attachment sections still appear only when content exists
- sub-agent prompt behavior remains unchanged with respect to full attachment
  omission
- memory-related prompt text uses `XXCODE.md` consistently in the main prompt and
  extraction prompt paths

### 11.4 Snapshot And Snapshot-Helper Updates

Prompt snapshots should be updated where the canonical wording is now expected.

Compatibility does not require snapshots to preserve old `CLAUDE.md` labels in
normal output because old-name support is an input compatibility rule, not a
preferred presentation rule.

The current snapshot scope is:

- `tests/prompt_snapshots/main_large_git.txt`
- `tests/prompt_snapshots/main_minimal.txt`
- `tests/prompt_snapshots/subagent_minimal.txt`
- `tests/prompt_snapshots/subagent_compact_git.txt`
- `tests/prompt_snapshots/main_large_claude.txt`

The first four files keep their filenames and update their canonical wording.

`main_large_claude.txt` should be renamed to
`main_large_project_instructions.txt` because it is an active internal test
artifact rather than a user-visible contract, and keeping `claude` in the name
would preserve stale terminology in maintained test code.

The corresponding helper and test identifiers should also be renamed:

- helper parameter `claude_md` -> `project_instructions`
- local variables such as `original_claude` -> `original_project_instructions`
- test names such as `test_main_prompt_large_claude_snapshot` -> a neutral
  project-instructions name
- snapshot file path literals in `tests/test_prompt_snapshots.py`, such as
  `assert_prompt_snapshot("main_large_claude.txt", text)`, must be updated to
  the new filename as part of the same change

### 11.5 Refactor-Safety Rule For Test Doubles

All test doubles that currently target `load_claude_md` must be updated to
target `load_project_instructions`.

There are two distinct risk classes:

- `monkeypatch.setattr(...)` sites should remain loud-fail checks against the
  new attribute name
- direct module-attribute assignment sites should be rewritten to use
  `monkeypatch.setattr(..., raising=True)` or another equally loud mechanism

This matters because direct module assignment can silently create a stale
attribute that no longer affects production code after the function rename,
especially in snapshot helpers that currently assign `builder.load_claude_md`
manually.

## 12. Documentation Policy

Documentation should be updated selectively rather than by naive global
replacement.

### 12.1 Documents To Update

The update-now list for this change set is:

- `docs/CODE_GUIDE.md`
- `docs/agent-loop-analysis.md`
- `docs/superpowers/specs/2026-06-03-context-engineering-evaluation-design.md`
- `docs/superpowers/plans/2026-06-03-context-engineering-evaluation.md`

These are treated as active because they either document the current runtime
behavior directly or describe an evaluation harness that remains in active test
use.

Where relevant, these documents should add a short note that `CLAUDE.md`
remains supported as a compatibility fallback.

### 12.2 Documents To Leave As Historical Record

The leave-as-historical list for this change set is:

- `docs/system-prompt-optimization-design.md`
- `docs/system-prompt-optimization-plan.md`
- `docs/system-prompt-optimization-phase2-design.md`
- `docs/system-prompt-optimization-phase2-plan.md`
- `docs/system-prompt-optimization-phase3-design.md`
- `docs/system-prompt-optimization-phase3-plan.md`
- `docs/system-prompt-optimization-phase4-design.md`
- `docs/system-prompt-optimization-phase4-plan.md`
- `docs/superpowers/plans/2026-06-04-test-redundancy-reduction.md`

These are retained as historical phase records or archived planning artifacts.
They can keep original `CLAUDE.md` wording unless a separate documentation pass
is explicitly scoped to normalize historical materials.

## 13. Implementation Outline

The implementation should proceed in this order:

1. implement the `XXCODE.md`-first, `CLAUDE.md`-fallback per-directory
   resolution rule in `src/xxcode/context/builder.py`
2. rename `load_claude_md` to `load_project_instructions` in
   `src/xxcode/context/builder.py` and update all direct callers there
3. update runtime-injected prompt strings in `assets/system-prompt.md`,
   `src/xxcode/context/builder.py`, `src/xxcode/memory/extraction.py`, and
   `src/xxcode/memory/prompts/extraction_system.md`
4. migrate canonical fixtures to `XXCODE.md` and add explicit fallback and
   same-directory precedence tests
5. rename snapshot helper identifiers, update snapshot file path references in
   `tests/test_prompt_snapshots.py`, rename the `main_large_claude.txt`
   snapshot, and refresh snapshot contents that encode canonical wording
6. update the active documentation list in section 12.1
7. run a repository grep for `load_claude_md` and active-runtime
   `CLAUDE.md` strings to catch stale maintained references

This order keeps the behavioral contract and the assertions synchronized.

## 14. Risks And Mitigations

### 14.1 Duplicate Loading Risk

Risk:

- loading both files from the same directory would duplicate or conflict
  instructions

Mitigation:

- enforce a single-file-per-directory rule with `XXCODE.md` preference

### 14.2 Half-Renamed User Experience

Risk:

- runtime accepts `XXCODE.md`, but prompts and docs still talk about
  `CLAUDE.md`

Mitigation:

- update active user-visible wording in the same change set

### 14.3 Memory-Extraction Prompt Drift

Risk:

- the main prompt, memory template, and extraction prompts could disagree about
  whether `CLAUDE.md` or `XXCODE.md` is the authoritative project-instruction
  source

Mitigation:

- update `src/xxcode/context/builder.py`, `src/xxcode/memory/extraction.py`,
  and `src/xxcode/memory/prompts/extraction_system.md` as one consistency chain

### 14.4 Historical Document Distortion

Risk:

- global search-and-replace would corrupt historical design intent

Mitigation:

- update active docs only and preserve archived historical wording where it is
  part of the record

### 14.5 Test-Double Drift After API Rename

Risk:

- snapshot helpers or other tests keep assigning or patching `load_claude_md`
  after production code switches to `load_project_instructions`

Mitigation:

- rename all test-double targets in the same change set
- prefer `monkeypatch.setattr(..., raising=True)` over raw module-attribute
  assignment for this seam
- grep for residual `load_claude_md` references before considering the rename
  complete

## 15. Acceptance Criteria

This work is complete when:

1. runtime prefers `XXCODE.md` and falls back to `CLAUDE.md`
2. only one file per directory is loaded during traversal
3. nearest-directory-first ordering still holds, with `\n\n---\n\n` preserved as
   the directory separator
4. the loader API is renamed to `load_project_instructions` with no maintained
   `load_claude_md` alias
5. canonical prompt wording references `XXCODE.md` across the main prompt,
   memory template, and extraction prompt chain
6. tests cover canonical, fallback, and same-directory precedence rules
   explicitly
7. active snapshot helpers and snapshot files use current terminology, including
   removal of the `main_large_claude` snapshot name and related identifiers
8. the active documentation list in section 12.1 describes `XXCODE.md` as
   canonical and `CLAUDE.md` as compatibility-only
