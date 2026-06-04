# Bash Security Parser Unification Design

> Version 0.2 | 2026-06-04 | Scope: remove duplicated Bash command parsing and safe-env normalization logic while preserving current security behavior

## 1. Objective

The repository currently contains multiple partial implementations of Bash
command parsing behavior:

- compound-command splitting
- safe environment-variable prefix stripping
- base-command extraction
- sudo/doas/pkexec detection

These behaviors are used by the Bash permission system, the speculative command
classifier, sandbox decisions, and some skill shell-safety paths.

The objective of this work is to unify the parsing primitives used by the Bash
security stack so the same command string is interpreted consistently across the
main decision points, without changing the current approval model.

## 2. User Request And Constraints

The user asked for:

- code-structure analysis
- identification of redundant logic
- optimization suggestions
- SDD planning before code changes
- TDD implementation when code changes begin

This design therefore assumes:

- implementation starts only after the design is written and reviewed
- tests are written first, then code is changed to satisfy them
- this phase should prefer a low-risk structural cleanup over a broad policy
  rewrite

## 3. Current State

The current codebase has a useful shared tokenizer in:

- `src/xxcode/tools/BashTool/_tokenizer.py`

However, equivalent parsing logic still exists elsewhere:

- `src/xxcode/security/classifier.py`
  - local `_split_pipeline`
  - local `_tokenize_command`
  - local `_extract_base_command`
  - local `SAFE_ENV_VARS`
- `src/xxcode/tools/BashTool/permissions.py`
  - canonical `SAFE_ENV_VARS`
  - `strip_safe_env_vars`
  - `strip_all_safe_env_prefixes`

This creates drift risk because the same shell command can be split or cleaned
slightly differently depending on which subsystem touches it first.

Examples of current divergence risk:

1. `security/classifier.py` has its own pipeline splitter, while
   `tools/BashTool/_tokenizer.py` already handles redirect-aware `&`
   semantics.
2. safe environment variables are duplicated in two modules.
3. tokenization rules are duplicated, but the classifier copy does not handle
   backslash-escaped whitespace the same way as `_tokenizer.py`.
4. base-command extraction rules are duplicated and may evolve differently.

## 4. Problem To Solve

This duplication creates three concrete problems:

### 4.1 Behavioral Drift

A command may be:

- auto-approved as safe by one parser
- flagged as needing permission by another
- excluded from sandboxing based on a third interpretation

Even when current tests pass, this architecture makes future changes fragile.

### 4.2 Harder Test Maintenance

Tests must validate equivalent behavior in multiple modules that implement the
same parsing rules differently.

### 4.3 Risky Future Security Changes

If a new shell edge case is fixed in only one parser, the system becomes less
predictable exactly where it should be most deterministic.

## 5. Design Goals

This work should:

1. establish one canonical implementation for command splitting
2. establish one canonical implementation for safe env-prefix stripping
3. make `security/classifier.py` consume shared primitives instead of owning
   duplicate parsing logic
4. preserve existing classification behavior unless a test explicitly documents
   an intended correction
5. keep the refactor small enough to validate with focused tests

## 6. Non-Goals

This design does not:

- redesign Bash security policy
- merge the entire Bash permission engine with skill shell execution policy
- change dangerous-command regex policy in `security/patterns.py`
- unify token-estimation logic in this phase
- move to a brand-new `shell/common.py` package in this phase
- rewrite all shell-related modules to use a single service object

## 7. Candidate Approaches

Three approaches were considered.

### 7.1 Conservative Consumer Refactor

Keep the existing shell primitive surface small, but consolidate all low-level
parsing helpers into the existing `_tokenizer.py` module and refactor
downstream consumers to reuse them.

Changes:

- `security/classifier.py` reuses `_tokenizer.split_pipeline`
- `security/classifier.py` reuses `_tokenizer.tokenize`
- `security/classifier.py` reuses `_tokenizer.extract_base_command`
- `permissions.py` and `security/classifier.py` both reuse safe env-prefix
  stripping from `_tokenizer.py`

Benefits:

- smallest behavior delta
- lowest review risk
- quickest TDD loop
- no new public module needed

Trade-offs:

- `_tokenizer.py` becomes the canonical home for more shell primitives
- this is still a primitive-level cleanup, not a full policy unification

### 7.2 New Shared Shell Common Module

Create a new shared module such as `src/xxcode/shell/common.py` and move all
parsing primitives there.

Benefits:

- cleaner ownership
- easier future extension

Trade-offs:

- larger import churn
- more test rewiring
- broader surface for regressions

### 7.3 Full Shell Safety Unification

Introduce a central shell safety policy layer used by BashTool, classifier, and
skills.

Benefits:

- strongest long-term architecture
- fewer policy drifts across execution paths

Trade-offs:

- too wide for a first refactor
- hard to validate in one low-risk change set
- would likely mix parser cleanup with policy changes

## 8. Chosen Strategy

This work adopts approach `7.1 Conservative Consumer Refactor`.

Why this is the best fit:

- it removes the highest-value duplication immediately
- it preserves existing module boundaries as much as possible
- it fits the user's request for structured, low-risk cleanup
- it leaves room for a later shell-policy unification phase

The principle for this phase is:

- centralize primitives first, centralize policy later

## 9. Detailed Design

### 9.1 Canonical Ownership

The following ownership model will be enforced:

- `src/xxcode/tools/BashTool/_tokenizer.py`
  - canonical safe environment-variable allowlist
  - canonical safe environment-variable prefix stripping
  - canonical command tokenization
  - canonical compound-command splitting
  - canonical base-token normalization
  - canonical base-command extraction
- `src/xxcode/tools/BashTool/permissions.py`
  - permission policy built on canonical shell primitives
- `src/xxcode/security/classifier.py`
  - classification policy only
  - no locally owned parsing implementation

### 9.2 Classifier Refactor

`src/xxcode/security/classifier.py` will be reduced to:

- command-class enums and result types
- safe command and safe subcommand allowlists
- classification policy built on shared parsing helpers

Planned changes:

1. remove the duplicated `SAFE_ENV_VARS` constant
2. replace local `strip_safe_env_vars` logic with shared stripping
3. replace local `_split_pipeline` implementation with shared splitter
4. replace local `_tokenize_command` implementation with shared tokenization
5. replace local `_extract_base_command` implementation with shared base
   extraction semantics, while preserving the classifier's need to know whether
   the original command was privilege-elevated

### 9.3 Privilege-Elevation Handling

One nuance remains: `security/classifier.py` currently needs a `has_sudo`
signal and a `subcommand` value, while `_tokenizer.extract_base_command()`
returns only the normalized base command.

This phase will solve that with a minimal helper inside `classifier.py`:

- strip all safe env prefixes first using the canonical helper
- tokenize the resulting `cleaned` command with canonical `_tokenizer.tokenize()`
- preserve the classifier's current tuple-construction semantics by stopping at
  redirect or command-terminator tokens such as `>`, `>>`, `<`, `2>`, `1>`,
  `&>`, `2>&1`, `1>&2`, `|`, `;`, and `&`
- detect privilege prefixes `sudo`, `doas`, and `pkexec`
- derive `base_command` from canonical `_tokenizer.extract_base_command()` on
  that same `cleaned` command so there is no double-stripping mismatch between
  the tuple path and the base-command path
- derive `subcommand` from the filtered token stream after any privilege
  prefix, preserving current classifier semantics where the second token may
  still be a flag such as `-la`
- do **not** skip remaining env-assignment tokens in the classifier wrapper;
  any env assignment still present after canonical safe-prefix stripping is
  treated as part of the command and therefore prevents accidental
  auto-approval of commands such as `LD_PRELOAD=evil.so ls`

This preserves low scope while still removing the duplicate full parser.

### 9.4 Public Compatibility

Some tests currently import:

- `security.classifier.strip_safe_env_vars`
- `security.classifier._split_pipeline`
- `security.classifier._tokenize_command`
- `security.classifier._extract_base_command`

This phase will preserve those call sites by turning them into thin wrappers
over the shared logic rather than deleting them outright.

That keeps external behavior stable while still removing implementation
duplication.

### 9.5 Safe Env Stripping Semantics

The current regex-based env stripping implementations are not identical and can
behave badly on edge cases such as quoted values with spaces.

This phase will define one canonical behavior in `_tokenizer.py`:

- strip only leading safe env assignments
- strip repeatedly while safe env prefixes continue
- strip only when a command remains after the env assignment
- leave the string unchanged for env-only input such as `FOO=bar`
- support quoted env values such as `FOO="bar baz" cmd`
- preserve unsafe env prefixes such as `LD_PRELOAD=evil.so ls` so downstream
  callers remain fail-closed
- fail closed: if the parser cannot confidently peel a safe env prefix, it
  must leave the command unchanged rather than guessing

This is a bounded behavior correction, not a policy expansion.

### 9.6 Import-Direction Decision

An import-graph audit was performed before revising this design:

- `tools/BashTool/security.py` does not import `security.classifier`
- `tools/BashTool/__init__.py` references `security.classifier` only inside
  method bodies, not at module import time
- no immediate runtime cycle was found for `classifier -> permissions`

Even so, `classifier -> permissions` would introduce unnecessary package-level
coupling because importing `xxcode.tools.BashTool.permissions` requires Python
to initialize the `xxcode.tools.BashTool` package first.

To keep the dependency direction cleaner, safe env constants and stripping
helpers should move into `_tokenizer.py`, and both `permissions.py` and
`classifier.py` should import them from there.

### 9.7 Documented Behavior Corrections

This refactor intentionally corrects a small number of existing shell-parsing
behaviors rather than preserving them:

- backslash-escaped whitespace in classifier tokenization is handled
  canonically
- quoted safe env values can be stripped correctly
- Windows path separators and executable suffixes are normalized consistently
- unsafe env prefixes no longer risk collapsing to the underlying safe command
  in the classifier path

These corrections must be locked down with tests before implementation.

## 10. Files In Scope

### 10.1 Production Files

- `src/xxcode/security/classifier.py`
- `src/xxcode/tools/BashTool/_tokenizer.py`
- `src/xxcode/tools/BashTool/permissions.py`

### 10.2 Test Files

- `tests/security/test_classifier.py`
- `tests/tools/test_permissions.py`

Additional focused tests may be added if the refactor exposes missing coverage
around:

- redirect-aware `&`
- quoted operators
- multi-prefix env stripping
- path-prefixed base commands

## 11. TDD Plan

Implementation will follow a red-green-refactor loop.

### 11.1 Red

Write or tighten tests that lock down the following explicit edge cases before
changing production code:

- background separator handling:
  - `make & npm run build`
- redirect-aware `&` handling:
  - `ls 2>&1 | grep foo`
- backslash-escaped whitespace:
  - `echo hello\ world`
- env-only input:
  - `FOO=bar`
- quoted env values with spaces:
  - `FOO="bar baz" cmd`
- safe env assignment with no trailing command:
  - `NODE_ENV=prod`
  - `NODE_ENV=prod   `
- unsafe env prefix before an otherwise safe command:
  - `LD_PRELOAD=evil.so ls`
- repeated safe env prefixes:
  - `NODE_ENV=test LANG=C python script.py`
- mixed compound operators:
  - `ls && echo ok || pwd; whoami & date`
- privilege prefixes in base extraction:
  - `sudo rm -rf /`
  - `doas ls`
  - `pkexec gedit`
- path-prefixed and extension-suffixed base commands:
  - `/usr/bin/git status`
  - `C:\tools\git.exe status`
- redirects interleaved with command tokens:
  - `git status > out.txt`
- known existing limitation to document but not fix in this phase:
  - `sudo -u root ls`

### 11.2 Green

Implement the smallest refactor that makes the above tests pass:

- move safe env stripping primitives into `_tokenizer.py`
- refactor `permissions.py` to reuse them
- refactor `classifier.py` wrappers to reuse canonical primitives

### 11.3 Refactor

After the tests pass:

- remove unused duplicated constants and parser branches
- simplify imports and helper naming
- keep the wrapper surface only where needed for compatibility
- verify that the classifier still fails closed on ambiguous input

## 12. Error Handling And Safety

This refactor must remain fail-closed:

- if parsing becomes ambiguous, the classifier should continue to return
  `NEEDS_PERMISSION` rather than widening auto-approval
- no path in this phase should turn a previously dangerous command into safe
  unless a test and code review explicitly justify it

## 13. Validation Strategy

The minimum validation set for this phase is:

- `pytest tests/security/test_classifier.py -v`
- `pytest tests/tools/test_permissions.py -v`

If changes touch shared tokenizer semantics in a broader way, also run:

- `pytest tests/tools/test_security_checks.py -v`

If env-stripping behavior is corrected for quoted values, add focused tests to
the relevant existing files rather than creating a new broad integration suite.

Success criteria:

1. no behavior regressions in focused shell parsing tests
2. classifier still marks clearly dangerous commands as non-safe
3. parser ownership is visibly reduced in `security/classifier.py`

## 14. Risks And Mitigations

### Risk 1: Hidden Behavior Drift

Even a structurally equivalent shared helper can behave differently on edge
cases.

Mitigation:

- preserve compatibility wrappers
- add focused tests before refactoring
- prefer wrapper-based reuse over signature changes

### Risk 2: Circular Imports

`security/classifier.py` will depend on `tools/BashTool` helpers.

Mitigation:

- keep imports one-directional
- avoid making `_tokenizer.py` depend on `security.classifier`

### Risk 3: Scope Expansion

It is tempting to also unify skill shell safety and token heuristics now.

Mitigation:

- explicitly defer those to later phases
- keep this phase limited to parser and normalization reuse

## 15. Follow-Up Work

This phase intentionally leaves two follow-up opportunities:

1. unify inline skill shell safety with the main BashTool policy path
2. unify token-estimation heuristics currently split across
   `context/auto.py`, `context/pipeline.py`, and `skills/persistence.py`

Those are worthwhile, but they should land as separate, testable changes.

An additional known limitation remains out of scope for this phase:

- `sudo -u root ls` and similar privilege-prefix flag forms are not normalized
  correctly by the current base-command extraction shape; this should be
  documented with an `xfail` or equivalent regression marker rather than fixed
  opportunistically here.
