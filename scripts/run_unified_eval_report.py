from __future__ import annotations

import asyncio
import sys
from argparse import ArgumentParser, Namespace
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for path in (str(SRC_ROOT), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from xxcode.benchmark import available_profiles, build_benchmark_report  # noqa: E402
from xxcode.benchmark.reporting import format_benchmark_report  # noqa: E402
from tests.benchmark.plugins.context import ContextBenchmarkPlugin  # noqa: E402
from tests.benchmark.plugins.memory import MemoryBenchmarkPlugin  # noqa: E402
from tests.benchmark.plugins.security import SecurityBenchmarkPlugin  # noqa: E402


REPORT_PREFIX = "unified-eval-report-"
REPORT_SUFFIX = ".txt"
TIER_CHOICES = ("smoke", "core", "stress")


def add_report_arguments(parser: ArgumentParser) -> ArgumentParser:
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
    parser.add_argument(
        "--baseline-profile",
        choices=available_profiles(),
        default=None,
        help="Optional fixed baseline profile for candidate-vs-baseline comparison.",
    )
    parser.add_argument(
        "--tier",
        dest="tiers",
        action="append",
        choices=TIER_CHOICES,
        default=None,
        help="Optional benchmark tier filter. May be provided multiple times.",
    )
    return parser


def build_parser(
    *,
    prog: str = "run_unified_eval_report",
    description: str = "Run the deterministic unified evaluation report.",
) -> ArgumentParser:
    parser = ArgumentParser(
        prog=prog,
        description=description,
    )
    return add_report_arguments(parser)


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


def _report_exit_code(report) -> int:
    return 0 if getattr(report, "passed", False) else 1


async def run_report(
    output_dir: Path,
    work_dir: Path,
    keep: int,
    *,
    baseline_profile: str | None = None,
    tiers: list[str] | None = None,
) -> int:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        plugins = [
            MemoryBenchmarkPlugin(),
            ContextBenchmarkPlugin(),
            SecurityBenchmarkPlugin(),
        ]
        report = await build_benchmark_report(
            plugins,
            baseline_plugins=plugins if baseline_profile is not None else None,
            baseline_profile=baseline_profile,
            tiers=tiers,
        )
        summary = format_benchmark_report(report)
        print(summary)
        output_path = build_output_path(output_dir, datetime.now())
        output_path.write_text(summary, encoding="utf-8")
        prune_old_reports(output_dir, keep)
        return _report_exit_code(report)
    except Exception as exc:
        print(f"unified eval report failed: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = (REPO_ROOT / args.output_dir).resolve()
    work_dir = (REPO_ROOT / args.work_dir).resolve()
    return asyncio.run(
        run_report(
            output_dir=output_dir,
            work_dir=work_dir,
            keep=args.keep,
            baseline_profile=args.baseline_profile,
            tiers=args.tiers,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
