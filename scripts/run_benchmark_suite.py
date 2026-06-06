from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for path in (str(SRC_ROOT), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

REPORT_SCRIPT_PATH = REPO_ROOT / "scripts" / "run_unified_eval_report.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location(
        "run_unified_eval_report",
        REPORT_SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load report script: {REPORT_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_parser() -> ArgumentParser:
    report_module = load_report_module()
    return report_module.build_parser(
        prog="run_benchmark_suite",
        description="Run benchmark tests and generate the unified benchmark report.",
    )


def parse_args(argv: list[str] | None = None) -> Namespace:
    return build_parser().parse_args(argv)


def run_benchmark_tests() -> int:
    print("==> Running benchmark tests")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/benchmark", "-q"],
        cwd=REPO_ROOT,
        check=False,
    )
    return int(result.returncode)


async def run_report_stage(
    *,
    output_dir: Path,
    work_dir: Path,
    keep: int,
    baseline_profile: str | None = None,
    tiers: list[str] | None = None,
) -> int:
    print("==> Generating benchmark report")
    report_module = load_report_module()
    return await report_module.run_report(
        output_dir=output_dir,
        work_dir=work_dir,
        keep=keep,
        baseline_profile=baseline_profile,
        tiers=tiers,
    )


async def run_suite(
    *,
    output_dir: Path,
    work_dir: Path,
    keep: int,
    baseline_profile: str | None = None,
    tiers: list[str] | None = None,
) -> int:
    try:
        test_exit_code = run_benchmark_tests()
        if test_exit_code != 0:
            print("benchmark tests failed; skipping report generation", file=sys.stderr)
            return 3
        report_exit_code = await run_report_stage(
            output_dir=output_dir,
            work_dir=work_dir,
            keep=keep,
            baseline_profile=baseline_profile,
            tiers=tiers,
        )
        return 2 if report_exit_code == 2 else report_exit_code
    except Exception as exc:
        print(f"benchmark suite runner failed: {exc}", file=sys.stderr)
        return 4


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        output_dir = (REPO_ROOT / args.output_dir).resolve()
        work_dir = (REPO_ROOT / args.work_dir).resolve()
        return asyncio.run(
            run_suite(
                output_dir=output_dir,
                work_dir=work_dir,
                keep=args.keep,
                baseline_profile=args.baseline_profile,
                tiers=args.tiers,
            )
        )
    except Exception as exc:
        print(f"benchmark suite runner failed: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
