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
