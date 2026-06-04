# XXCODE.md Rename Design

> Version 0.1 | 2026-06-04 | Scope: rename the project-instruction file from `CLAUDE.md` to `XXCODE.md` while preserving low-risk runtime compatibility

## 1. Objective

The repository currently treats `CLAUDE.md` as the project-instruction file that
can be loaded into the main agent prompt.

The goal of this work is to:

- make `XXCODE.md` the new canonical project-instruction filename
- preserve compatibility with existing projects that still use `CLAUDE.md`
- align runtime behavior, prompt wording, tests, and active documentation
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
- tests that create `CLAUDE.md` fixtures and assert prompt wording
- prompt snapshots that embed the old name
- design and guide documents that describe the current behavior

The runtime loader currently walks upward from the current working directory and
collects `CLAUDE.md` files by directory.

The main behavior risks in the current state are:

- the file name is now project-branded incorrectly for this repository
- a partial rename would leave runtime and prompt wording inconsistent
- supporting both names naively could cause duplicate content from the same
  directory

## 4. Problem To Solve

We need a deterministic migration contract for project-instruction files.

The contract must answer:

1. which filename is canonical going forward
2. how the loader behaves when only the old name exists
3. how the loader behaves when both names exist in the same directory
4. which user-visible strings should be updated now
5. which historical documents should remain untouched

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

## 10. Prompt And Policy Wording Changes

The user-visible prompt wording should move to the new canonical name now.

This includes:

- project-instruction priority wording in the system prompt
- trust wording that explains the role of the project-instruction file
- attachment headings such as `Project Instructions (XXCODE.md)`
- prompt assertions and snapshots that check those strings

The intent is that users interacting with the system see only the new official
name unless the repository is explicitly discussing backward compatibility.

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

### 11.2 Prompt Contract Cases

The test suite should also confirm:

- prompt output uses `XXCODE.md` in canonical wording
- project-instruction attachment sections still appear only when content exists
- sub-agent prompt behavior remains unchanged with respect to full attachment
  omission

### 11.3 Snapshot Updates

Prompt snapshots should be updated where the canonical wording is now expected.

Compatibility does not require snapshots to preserve old `CLAUDE.md` labels in
normal output because old-name support is an input compatibility rule, not a
preferred presentation rule.

## 12. Documentation Policy

Documentation should be updated selectively rather than by naive global
replacement.

### 12.1 Documents To Update

These should move to `XXCODE.md` wording:

- active implementation guides
- current architecture or prompt-behavior documentation
- tests or plans that are still used as operational references

Where relevant, these documents should add a short note that `CLAUDE.md`
remains supported as a compatibility fallback.

### 12.2 Documents To Leave As Historical Record

These should generally keep original `CLAUDE.md` wording:

- old design discussions describing past behavior
- external comparison notes
- historical plans whose point is to record what the system used at that time

This avoids rewriting history and keeps prior design context intelligible.

## 13. Implementation Outline

The implementation should proceed in this order:

1. update the runtime loader in `src/xxcode/context/builder.py`
2. update prompt headings and policy wording to `XXCODE.md`
3. update unit tests and add compatibility-specific cases
4. refresh prompt snapshots that encode canonical wording
5. update active implementation-facing docs that describe the current contract

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

### 14.3 Historical Document Distortion

Risk:

- global search-and-replace would corrupt historical design intent

Mitigation:

- update active docs only and preserve archived historical wording where it is
  part of the record

## 15. Acceptance Criteria

This work is complete when:

1. runtime prefers `XXCODE.md` and falls back to `CLAUDE.md`
2. only one file per directory is loaded during traversal
3. nearest-directory-first ordering still holds
4. canonical prompt wording references `XXCODE.md`
5. tests cover the compatibility and precedence rules explicitly
6. active implementation-facing docs describe `XXCODE.md` as canonical and
   `CLAUDE.md` as compatibility-only
