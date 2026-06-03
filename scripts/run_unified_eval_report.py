from __future__ import annotations

import asyncio
import sys
from argparse import ArgumentParser, Namespace
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.memory.helpers.report_eval import (  # noqa: E402
    UnifiedEvaluationReport,
    build_unified_report,
    format_unified_report,
)


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
