from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "run_benchmark_suite.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location("run_benchmark_suite", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_args_accepts_report_options():
    script = _load_script_module()
    args = script.parse_args(
        [
            "--output-dir",
            "tmp/reports",
            "--work-dir",
            "tmp/work",
            "--keep",
            "2",
            "--baseline-profile",
            "memory_off",
            "--tier",
            "smoke",
            "--tier",
            "core",
        ]
    )

    assert args.output_dir == "tmp/reports"
    assert args.work_dir == "tmp/work"
    assert args.keep == 2
    assert args.baseline_profile == "memory_off"
    assert args.tiers == ["smoke", "core"]


def test_load_report_module_reads_report_script_by_path():
    script = _load_script_module()
    report_module = script.load_report_module()

    assert hasattr(report_module, "run_report")
    assert hasattr(report_module, "build_parser")


def test_run_benchmark_tests_uses_sys_executable(monkeypatch):
    script = _load_script_module()
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(script.subprocess, "run", _fake_run)

    exit_code = script.run_benchmark_tests()

    assert exit_code == 0
    assert captured["cmd"][:3] == [script.sys.executable, "-m", "pytest"]
    assert captured["cmd"][3:] == ["tests/benchmark", "-q"]


@pytest.mark.asyncio
async def test_run_suite_returns_three_and_skips_report_when_tests_fail(monkeypatch, tmp_path):
    script = _load_script_module()

    monkeypatch.setattr(script, "run_benchmark_tests", lambda: 1)

    async def _boom(**kwargs):
        raise AssertionError("report should not run")

    monkeypatch.setattr(script, "run_report_stage", _boom)

    exit_code = await script.run_suite(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
    )

    assert exit_code == 3


@pytest.mark.asyncio
async def test_run_suite_forwards_report_kwargs_after_test_success(monkeypatch, tmp_path):
    script = _load_script_module()
    monkeypatch.setattr(script, "run_benchmark_tests", lambda: 0)
    captured: dict[str, object] = {}

    async def _fake_run_report_stage(**kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(script, "run_report_stage", _fake_run_report_stage)

    exit_code = await script.run_suite(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
        baseline_profile="memory_off",
        tiers=["smoke"],
    )

    assert exit_code == 1
    assert captured["baseline_profile"] == "memory_off"
    assert captured["tiers"] == ["smoke"]


@pytest.mark.asyncio
async def test_run_suite_returns_four_on_wrapper_exception(monkeypatch, tmp_path, capsys):
    script = _load_script_module()

    def _boom():
        raise RuntimeError("wrapper broke")

    monkeypatch.setattr(script, "run_benchmark_tests", _boom)

    exit_code = await script.run_suite(
        output_dir=tmp_path / "reports",
        work_dir=tmp_path / "work",
        keep=4,
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "benchmark suite runner failed:" in captured.err
