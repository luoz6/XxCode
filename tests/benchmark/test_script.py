from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


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


@pytest.mark.asyncio
async def test_run_report_writes_file_and_returns_zero_for_passing_report(
    tmp_path,
    capsys,
    monkeypatch,
):
    script = _load_script_module()

    class _FakeReport:
        passed = True

    async def _fake_build_benchmark_report(_plugins, **kwargs):
        assert kwargs["baseline_profile"] is None
        return _FakeReport()

    monkeypatch.setattr(script, "build_benchmark_report", _fake_build_benchmark_report)
    monkeypatch.setattr(script, "format_benchmark_report", lambda _: "# Benchmark Report")

    exit_code = await script.run_report(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
    )

    captured = capsys.readouterr()
    written_files = list((tmp_path / "reports").glob("unified-eval-report-*.txt"))

    assert exit_code == 0
    assert captured.out.strip() == "# Benchmark Report"
    assert len(written_files) == 1
    assert written_files[0].read_text(encoding="utf-8") == "# Benchmark Report"


@pytest.mark.asyncio
async def test_run_report_returns_one_for_threshold_failure(tmp_path, monkeypatch):
    script = _load_script_module()

    class _FakeReport:
        passed = False

    async def _fake_build_benchmark_report(_plugins, **kwargs):
        assert kwargs["baseline_profile"] is None
        return _FakeReport()

    monkeypatch.setattr(script, "build_benchmark_report", _fake_build_benchmark_report)
    monkeypatch.setattr(script, "format_benchmark_report", lambda _: "# Benchmark Report")

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

    async def _boom(_plugins, **kwargs):
        assert kwargs["baseline_profile"] is None
        raise RuntimeError("boom")

    monkeypatch.setattr(script, "build_benchmark_report", _boom)

    exit_code = await script.run_report(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
    )

    captured = capsys.readouterr()

    assert exit_code == 2
    assert "boom" in captured.err


def test_main_resolves_relative_paths_and_returns_asyncio_result(monkeypatch):
    script = _load_script_module()

    async def _fake_run_report(
        output_dir: Path,
        work_dir: Path,
        keep: int,
        *,
        baseline_profile: str | None = None,
        tiers: list[str] | None = None,
    ) -> int:
        assert output_dir == script.REPO_ROOT / "docs" / "reports"
        assert work_dir == script.REPO_ROOT / ".tmp" / "unified-eval-run"
        assert keep == 4
        assert baseline_profile is None
        assert tiers is None
        return 0

    monkeypatch.setattr(script, "run_report", _fake_run_report)

    assert script.main([]) == 0


def test_script_runs_as_standalone_process_and_writes_report(tmp_path):
    output_dir = tmp_path / "reports"
    work_dir = tmp_path / "work"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

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
        encoding="utf-8",
        env=env,
        check=False,
    )

    written_files = list(output_dir.glob("unified-eval-report-*.txt"))

    assert result.returncode in {0, 1}
    assert "# 基准评测报告" in result.stdout
    assert len(written_files) == 1


def test_parse_args_accepts_baseline_profile():
    script = _load_script_module()

    args = script.parse_args(["--baseline-profile", "memory_off"])

    assert args.baseline_profile == "memory_off"


def test_parse_args_accepts_multiple_tiers():
    script = _load_script_module()

    args = script.parse_args(["--tier", "smoke", "--tier", "core"])

    assert args.tiers == ["smoke", "core"]


@pytest.mark.asyncio
async def test_run_report_forwards_baseline_profile(tmp_path, monkeypatch):
    script = _load_script_module()

    class _FakeReport:
        passed = True

    async def _fake_build_benchmark_report(_plugins, **kwargs):
        assert kwargs["baseline_profile"] == "memory_off"
        assert kwargs["baseline_plugins"] is not None
        return _FakeReport()

    monkeypatch.setattr(script, "build_benchmark_report", _fake_build_benchmark_report)
    monkeypatch.setattr(script, "format_benchmark_report", lambda _: "# Benchmark Report")

    exit_code = await script.run_report(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
        baseline_profile="memory_off",
    )

    assert exit_code == 0


@pytest.mark.asyncio
async def test_run_report_forwards_tiers(tmp_path, monkeypatch):
    script = _load_script_module()

    class _FakeReport:
        passed = True

    async def _fake_build_benchmark_report(_plugins, **kwargs):
        assert kwargs["tiers"] == ["smoke"]
        return _FakeReport()

    monkeypatch.setattr(script, "build_benchmark_report", _fake_build_benchmark_report)
    monkeypatch.setattr(script, "format_benchmark_report", lambda _: "# Benchmark Report")

    exit_code = await script.run_report(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
        tiers=["smoke"],
    )

    assert exit_code == 0
