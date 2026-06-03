# Unified Evaluation Report Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight local script that runs the deterministic unified memory/context evaluation report, prints the Chinese summary, writes a timestamped report file, prunes old files, and returns meaningful exit codes.

**Architecture:** Keep all report semantics in `XxCode/tests/memory/helpers/report_eval.py` and add a thin wrapper script under `XxCode/scripts/run_unified_eval_report.py`. The script bootstraps the repository root onto `sys.path`, calls the existing async `build_unified_report` helper through `asyncio.run`, prints the formatted report, writes a timestamped file under `docs/reports`, and prunes older files beyond the retention limit. Tests load the script module directly from its file path with `importlib.util` so the script can stay a simple standalone file rather than requiring a packaged `scripts` module.

**Tech Stack:** Python 3.11, pytest, pytest-asyncio, argparse, asyncio, pathlib, datetime, importlib.util, subprocess, existing test-local unified report helper

---

## File Structure

- Command working directory: run all commands from `F:\agent\XxCode`. The actual repository root is `F:\agent\XxCode\XxCode`, so file paths in this plan include the leading `XxCode/` directory. Git commands use the prefix `git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode`.
- Create: `XxCode/scripts/run_unified_eval_report.py`
  Responsibility: repository-root bootstrap, argument parsing, async unified report execution, stdout/stderr output, timestamped file writing, retention pruning, and process exit codes.
- Create: `XxCode/tests/memory/test_unified_eval_report_script.py`
  Responsibility: TDD coverage for script path helpers, retention pruning, `run_report` success/failure/error exit codes, file writing, and one subprocess smoke test for the standalone script entry point.
- Reuse without modification: `XxCode/tests/memory/helpers/report_eval.py`
  Responsibility: actual unified evaluation aggregation and Chinese report formatting.

## Task 1: Add Script Path Helpers And Retention Pruning

**Files:**
- Create: `XxCode/scripts/run_unified_eval_report.py`
- Create: `XxCode/tests/memory/test_unified_eval_report_script.py`

- [ ] **Step 1: Write failing tests for repository-root loading, output naming, and pruning**

Create `XxCode/tests/memory/test_unified_eval_report_script.py` with:

```python
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "run_unified_eval_report.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_unified_eval_report",
        SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_output_path_uses_timestamped_filename(tmp_path):
    script = _load_script_module()

    output_path = script.build_output_path(
        tmp_path,
        datetime(2026, 6, 3, 14, 35, 22),
    )

    assert output_path == tmp_path / "unified-eval-report-20260603-143522.txt"


def test_prune_old_reports_keeps_only_newest_matching_files(tmp_path):
    script = _load_script_module()
    filenames = [
        "unified-eval-report-20260603-140000.txt",
        "unified-eval-report-20260603-140100.txt",
        "unified-eval-report-20260603-140200.txt",
        "unified-eval-report-20260603-140300.txt",
        "unified-eval-report-20260603-140400.txt",
        "other-report.txt",
    ]
    for name in filenames:
        (tmp_path / name).write_text(name, encoding="utf-8")

    script.prune_old_reports(tmp_path, keep=4)

    remaining = sorted(path.name for path in tmp_path.iterdir())
    assert remaining == [
        "other-report.txt",
        "unified-eval-report-20260603-140100.txt",
        "unified-eval-report-20260603-140200.txt",
        "unified-eval-report-20260603-140300.txt",
        "unified-eval-report-20260603-140400.txt",
    ]
```

- [ ] **Step 2: Run the Task 1 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_unified_eval_report_script.py -v
```

Expected: FAIL with `FileNotFoundError` or import failure because `XxCode/scripts/run_unified_eval_report.py` does not exist.

- [ ] **Step 3: Add the minimal script with bootstrap, naming, and pruning helpers**

Create `XxCode/scripts/run_unified_eval_report.py` with:

```python
from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


REPORT_PREFIX = "unified-eval-report-"
REPORT_SUFFIX = ".txt"


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="run_unified_eval_report",
        description="Run the deterministic unified evaluation report.",
    )
    parser.add_argument(
        "--output-dir",
        default="docs/reports",
        help="Directory for timestamped report files.",
    )
    parser.add_argument(
        "--work-dir",
        default=".tmp/unified-eval-run",
        help="Temporary working directory for benchmark materialization.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=4,
        help="How many timestamped report files to retain.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> Namespace:
    return build_parser().parse_args(argv)


def build_output_path(output_dir: Path, now: datetime) -> Path:
    return output_dir / f"{REPORT_PREFIX}{now:%Y%m%d-%H%M%S}{REPORT_SUFFIX}"


def prune_old_reports(output_dir: Path, keep: int) -> None:
    report_files = sorted(
        output_dir.glob(f"{REPORT_PREFIX}*{REPORT_SUFFIX}"),
        key=lambda path: path.name,
        reverse=True,
    )
    for old_path in report_files[keep:]:
        old_path.unlink()
```

- [ ] **Step 4: Run the Task 1 tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_unified_eval_report_script.py -v
```

Expected: PASS for the two new script helper tests.

- [ ] **Step 5: Commit Task 1**

Run:

```powershell
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode add scripts/run_unified_eval_report.py tests/memory/test_unified_eval_report_script.py
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode commit -m "test: add unified eval script path helpers"
```

## Task 2: Add Async Report Execution, File Writing, And Exit Codes

**Files:**
- Modify: `XxCode/scripts/run_unified_eval_report.py`
- Modify: `XxCode/tests/memory/test_unified_eval_report_script.py`

- [ ] **Step 1: Extend tests for success, threshold failure, and runtime failure**

Append to `XxCode/tests/memory/test_unified_eval_report_script.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_run_report_writes_file_and_returns_zero_for_passing_report(
    tmp_path,
    capsys,
    monkeypatch,
):
    script = _load_script_module()
    report = object()

    async def _fake_build_unified_report(work_dir: Path):
        assert work_dir == tmp_path / "work"
        return report

    def _fake_format_unified_report(actual_report: object) -> str:
        assert actual_report is report
        return "统一评测报告 通过=True 失败项=0"

    monkeypatch.setattr(script, "build_unified_report", _fake_build_unified_report)
    monkeypatch.setattr(script, "format_unified_report", _fake_format_unified_report)
    monkeypatch.setattr(script, "_report_exit_code", lambda _: 0)

    exit_code = await script.run_report(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
    )

    captured = capsys.readouterr()
    written_files = list((tmp_path / "reports").glob("unified-eval-report-*.txt"))

    assert exit_code == 0
    assert captured.out.strip() == "统一评测报告 通过=True 失败项=0"
    assert len(written_files) == 1
    assert written_files[0].read_text(encoding="utf-8") == "统一评测报告 通过=True 失败项=0"


@pytest.mark.asyncio
async def test_run_report_returns_one_for_threshold_failure(tmp_path, monkeypatch):
    script = _load_script_module()

    async def _fake_build_unified_report(work_dir: Path):
        return object()

    monkeypatch.setattr(script, "build_unified_report", _fake_build_unified_report)
    monkeypatch.setattr(script, "format_unified_report", lambda _: "统一评测报告 通过=False 失败项=1")
    monkeypatch.setattr(script, "_report_exit_code", lambda _: 1)

    exit_code = await script.run_report(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
    )

    assert exit_code == 1


@pytest.mark.asyncio
async def test_run_report_returns_two_on_unexpected_runtime_failure(
    tmp_path,
    capsys,
    monkeypatch,
):
    script = _load_script_module()

    async def _boom(work_dir: Path):
        raise RuntimeError("boom")

    monkeypatch.setattr(script, "build_unified_report", _boom)

    exit_code = await script.run_report(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
    )

    captured = capsys.readouterr()

    assert exit_code == 2
    assert "boom" in captured.err
```

- [ ] **Step 2: Run the Task 2 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_unified_eval_report_script.py -v
```

Expected: FAIL because `run_report`, `_report_exit_code`, `build_unified_report`, and `format_unified_report` are not exposed by the script yet.

- [ ] **Step 3: Add async orchestration and exit-code mapping**

Update `XxCode/scripts/run_unified_eval_report.py` imports and append:

```python
import asyncio

from tests.memory.helpers.report_eval import (
    UnifiedEvaluationReport,
    build_unified_report,
    format_unified_report,
)
```

Then append to `XxCode/scripts/run_unified_eval_report.py`:

```python
def _report_exit_code(report: UnifiedEvaluationReport) -> int:
    return 0 if report.passed else 1


async def run_report(output_dir: Path, work_dir: Path, keep: int) -> int:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        report = await build_unified_report(work_dir)
        summary = format_unified_report(report)
        print(summary)
        output_path = build_output_path(output_dir, datetime.now())
        output_path.write_text(summary, encoding="utf-8")
        prune_old_reports(output_dir, keep)
        return _report_exit_code(report)
    except Exception as exc:
        print(f"unified eval report failed: {exc}", file=sys.stderr)
        return 2
```

- [ ] **Step 4: Run the Task 2 tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_unified_eval_report_script.py -v
```

Expected: PASS for the five script tests.

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode add scripts/run_unified_eval_report.py tests/memory/test_unified_eval_report_script.py
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode commit -m "feat: add unified eval script runner"
```

## Task 3: Add Main Entry Point, Argument Handling, And Subprocess Smoke Test

**Files:**
- Modify: `XxCode/scripts/run_unified_eval_report.py`
- Modify: `XxCode/tests/memory/test_unified_eval_report_script.py`

- [ ] **Step 1: Add failing tests for `main()` and standalone subprocess execution**

Append to `XxCode/tests/memory/test_unified_eval_report_script.py`:

```python
import subprocess
import sys


def test_main_resolves_relative_paths_and_returns_asyncio_result(tmp_path, monkeypatch):
    script = _load_script_module()

    async def _fake_run_report(output_dir: Path, work_dir: Path, keep: int) -> int:
        assert output_dir == script.REPO_ROOT / "docs" / "reports"
        assert work_dir == script.REPO_ROOT / ".tmp" / "unified-eval-run"
        assert keep == 4
        return 0

    monkeypatch.setattr(script, "run_report", _fake_run_report)

    assert script.main([]) == 0


def test_script_runs_as_standalone_process_and_writes_report(tmp_path):
    output_dir = tmp_path / "reports"
    work_dir = tmp_path / "work"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--output-dir",
            str(output_dir),
            "--work-dir",
            str(work_dir),
            "--keep",
            "4",
        ],
        cwd=SCRIPT_PATH.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    written_files = list(output_dir.glob("unified-eval-report-*.txt"))

    assert result.returncode == 0
    assert "统一评测报告 通过=True 失败项=0" in result.stdout
    assert len(written_files) == 1
```

- [ ] **Step 2: Run the Task 3 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_unified_eval_report_script.py -v
```

Expected: FAIL because `main([])` is not implemented and the script does not yet call `SystemExit(main())`.

- [ ] **Step 3: Add `main()` and shell entry point**

Append to `XxCode/scripts/run_unified_eval_report.py`:

```python
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = (REPO_ROOT / args.output_dir).resolve()
    work_dir = (REPO_ROOT / args.work_dir).resolve()
    return asyncio.run(
        run_report(
            output_dir=output_dir,
            work_dir=work_dir,
            keep=args.keep,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run script tests and the direct script command**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_unified_eval_report_script.py -v
```

Expected: PASS for the seven script tests.

Run:

```powershell
Set-Location XxCode
py -3.11 scripts/run_unified_eval_report.py
```

Expected: Chinese unified report prints to the terminal, a timestamped file appears under `docs/reports/`, and the process exits with code `0`.

- [ ] **Step 5: Commit Task 3**

Run:

```powershell
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode add scripts/run_unified_eval_report.py tests/memory/test_unified_eval_report_script.py
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode commit -m "feat: add standalone unified eval script entry point"
```

## Task 4: Run Regression And Manual Retention Smoke

**Files:**
- Reuse without modification: `XxCode/scripts/run_unified_eval_report.py`
- Reuse without modification: `XxCode/tests/memory/test_unified_eval_report_script.py`

- [ ] **Step 1: Run script regression together with unified report and memory/context suites**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_unified_eval_report_script.py XxCode/tests/memory/test_memory_context_eval_report.py XxCode/tests/memory/test_recall_eval.py XxCode/tests/memory/test_recall_stability.py XxCode/tests/memory/test_index_eval.py XxCode/tests/memory/test_extraction_eval.py XxCode/tests/memory/test_effectiveness_eval.py XxCode/tests/context/test_context_engineering_eval.py XxCode/tests/context/test_context_engineering_stability.py -v
```

Expected: PASS. The script tests pass, the unified report tests remain green, and the underlying memory/context benchmarks remain unchanged.

- [ ] **Step 2: Run a manual retention smoke with five timestamped files**

Run:

```powershell
Set-Location XxCode
$reportDir = Join-Path (Get-Location) "docs\\reports"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
"20260603-140000","20260603-140100","20260603-140200","20260603-140300" | ForEach-Object {
    Set-Content -LiteralPath (Join-Path $reportDir ("unified-eval-report-" + $_ + ".txt")) -Value $_
}
py -3.11 scripts/run_unified_eval_report.py
Get-ChildItem -LiteralPath $reportDir -Filter "unified-eval-report-*.txt" | Sort-Object Name | Select-Object -ExpandProperty Name
```

Expected: the command prints the Chinese report, leaves only four `unified-eval-report-*.txt` files, and the oldest timestamped file is absent from the final listing.

- [ ] **Step 3: Record the final state**

No code changes are expected in this task. If Steps 1 and 2 both pass, do not create an additional commit.

## Self-Review Checklist

- Spec coverage:
  - standalone script path and repository-root invocation: covered in Task 3
  - `sys.path` bootstrap for importing `tests.memory.helpers.report_eval`: covered in Task 1 and exercised again in the subprocess smoke test in Task 3
  - Chinese report printing: covered in Task 2 and Task 3
  - timestamped file writing under `docs/reports`: covered in Task 2 and Task 3
  - retention cap of four files: covered in Task 1 and Task 4
  - exit codes `0`, `1`, and `2`: covered in Task 2
  - no production CLI changes: maintained throughout
- Placeholder scan:
  - no placeholder markers or vague “add validation” instructions remain
  - every command is explicit and runnable from `F:\agent\XxCode`
- Type consistency:
  - script filename is consistently `run_unified_eval_report.py`
  - helper functions keep the names defined in the spec
  - async orchestration consistently flows through `run_report` and `main`
