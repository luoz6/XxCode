# Unified Evaluation Report Script Design

> Version 0.1 | 2026-06-03 | Scope: lightweight script wrapper for the deterministic memory/context unified evaluation report

## 1. Objective

The repository already has a deterministic unified evaluation report builder in
`tests/memory/helpers/report_eval.py`, but it is currently convenient only for
pytest or ad hoc inline Python execution.

This design adds a lightweight local script that:

- runs the unified report directly from the repository root
- prints the Chinese report summary to the terminal
- writes a timestamped report file under `docs/reports/`
- keeps only the newest four report files
- returns a non-zero exit code when threshold checks fail

The goal is faster daily usage, not a production CLI redesign.

## 2. Current State

Current report entry points:

- `tests/memory/helpers/report_eval.py`
  - `build_unified_report`
  - `format_unified_report`
- `tests/memory/test_memory_context_eval_report.py`

Current manual usage requires either:

- running pytest
- running an inline Python snippet that imports the helper

That is functional, but slower and less ergonomic than a dedicated script.

## 3. Problem To Solve

The user wants a single local command that can be run repeatedly without
rewriting inline Python.

That command should also manage report files automatically:

- write a new timestamped report file each time
- retain history
- cap history at four files

The script must preserve the current deterministic evaluation semantics rather
than reimplement them.

## 4. Design Goals

This design should:

1. keep implementation small and local
2. reuse the existing unified report helper instead of duplicating logic
3. print the same Chinese report summary that the helper already formats
4. write reports to `docs/reports/`
5. keep only the newest four report files
6. return `0` on pass and `1` on threshold failure
7. avoid touching the production `xxcode` CLI in version 0.1

## 5. Non-Goals

This design does not:

- add a new `xxcode` subcommand
- move the report helper out of `tests/`
- redesign unified report thresholds
- output JSON
- upload artifacts anywhere
- support arbitrary scheduling or CI orchestration in this phase

## 6. Chosen Approach

The chosen approach is a standalone script under `scripts/`:

- `XxCode/scripts/run_unified_eval_report.py`

This script should be a thin wrapper over the existing helper API.

Why this approach:

- smaller surface area than changing `src/xxcode/main.py`
- clearer daily entry point than importing test helpers through an inline Python snippet
- easy to evolve in a deferred phase into a richer tool if needed

## 7. Script Contract

### 7.1 Invocation

From repository root:

```powershell
py -3.11 scripts/run_unified_eval_report.py
```

This script is expected to be run from the repository root
`F:\agent\XxCode\XxCode`.

The script should not assume `PYTHONPATH` is already configured. It should
bootstrap imports by inserting the repository root into `sys.path` before
importing `tests.memory.helpers.report_eval`.

### 7.2 Behavior

On each run the script should:

1. build the unified evaluation report
2. format it with `format_unified_report`
3. print the report text to stdout
4. write the same text to a timestamped file
5. prune older report files beyond the retention limit
6. exit with status `0` if `report.passed` is true
7. exit with status `1` if `report.passed` is false

If the report build itself crashes due to an unexpected runtime error, the
script should exit with status `2` after printing a concise error message to
stderr. This keeps threshold failure separate from script failure.

### 7.3 Default Paths

Default output directory:

- `docs/reports/`

Default working directory for temporary benchmark materialization:

- `.tmp/unified-eval-run`

The script may create these directories if they do not already exist.

### 7.4 Output File Naming

Each run should create a timestamped report file with this pattern:

- `unified-eval-report-YYYYMMDD-HHMMSS.txt`

Example:

- `docs/reports/unified-eval-report-20260603-143522.txt`

The timestamp should use the local system time of the machine running the
script.

## 8. Retention Policy

The script should retain only the newest four files matching:

- `unified-eval-report-*.txt`

Retention rules:

1. create the new report file first
2. list all matching files in the output directory
3. sort by filename timestamp descending
4. keep the newest four
5. delete older files beyond that limit

Filename-based ordering is sufficient because the timestamp format is
lexicographically sortable.

This policy should affect only the unified report files matching the exact
prefix. Other files in `docs/reports/` must not be touched.

## 9. Parameters

Version 0.1 should expose only a minimal parameter set:

- `--output-dir`
  - default: `docs/reports`
- `--work-dir`
  - default: `.tmp/unified-eval-run`
- `--keep`
  - default: `4`

These parameters are enough for local flexibility without turning the script
into a full CLI subsystem.

## 10. Internal Structure

The script should remain small. Recommended functions:

```python
def build_output_path(output_dir: Path, now: datetime) -> Path
def prune_old_reports(output_dir: Path, keep: int) -> None
async def run_report(output_dir: Path, work_dir: Path, keep: int) -> int
def main() -> int
```

`main()` should call `asyncio.run(run_report(output_dir, work_dir, keep))` so
the script can drive the existing async `build_unified_report` helper safely from a normal shell
entry point.

Recommended data flow:

1. parse arguments
2. resolve the repository root from `__file__`
3. prepend that repository root to `sys.path`
4. resolve paths relative to repository root
5. call `build_unified_report(work_dir)`
6. call `format_unified_report(report)`
7. print report text
8. write report file
9. prune old files
10. return exit code

The script should keep business logic thin and delegate evaluation semantics to
`tests/memory/helpers/report_eval.py`.

## 11. Testing Strategy

Implementation should follow TDD.

Recommended tests:

1. script helper builds the expected timestamped filename
2. prune logic keeps only the newest four report files
3. script returns `0` when a passing report is produced
4. script returns `1` when a failing report is produced
5. script writes the report text to disk

Version 0.1 does not need a subprocess-heavy end-to-end shell test if the same
behavior can be covered by calling script functions directly from pytest.

At the current benchmark corpus size, script runtime is acceptable for daily
local use. On this machine, the direct unified report smoke run completed in
roughly 3 seconds, while the broader memory/context regression run completed in
under 5 seconds. Version 0.1 therefore does not need caching or incremental
execution.

Recommended test file:

- `XxCode/tests/memory/test_unified_eval_report_script.py`

## 12. File Boundaries

Version 0.1 should add:

- `XxCode/scripts/run_unified_eval_report.py`
- `XxCode/tests/memory/test_unified_eval_report_script.py`

No production source files under `src/xxcode/` should be modified in this
phase.

## 13. Error Handling

The script should fail clearly when:

- the output directory cannot be created
- the work directory cannot be created
- the report build raises an exception
- a report file cannot be written
- old files cannot be pruned

Error output should be concise and should identify the failing path or step.

Unexpected runtime failures should map to exit code `2`.

## 14. Success Criteria

This design is successful when the user can run one command from the repository
root and get all of the following:

- the Chinese unified evaluation report printed to the terminal
- a timestamped report file under `docs/reports/`
- automatic retention of only the newest four report files
- exit code `0` on pass
- exit code `1` on threshold failure

## 15. Deferred Work

Deferred phases may add:

- a formal `xxcode` subcommand
- JSON output
- `--no-fail-on-threshold`
- richer artifact metadata
- CI-specific wrappers

Those are intentionally deferred to keep version 0.1 small and direct.
