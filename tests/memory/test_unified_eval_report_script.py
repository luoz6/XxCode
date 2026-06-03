from __future__ import annotations

import importlib.util
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
    assert (
        written_files[0].read_text(encoding="utf-8")
        == "统一评测报告 通过=True 失败项=0"
    )


@pytest.mark.asyncio
async def test_run_report_returns_one_for_threshold_failure(tmp_path, monkeypatch):
    script = _load_script_module()

    async def _fake_build_unified_report(work_dir: Path):
        return object()

    monkeypatch.setattr(script, "build_unified_report", _fake_build_unified_report)
    monkeypatch.setattr(
        script,
        "format_unified_report",
        lambda _: "统一评测报告 通过=False 失败项=1",
    )
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
