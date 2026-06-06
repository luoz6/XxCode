from __future__ import annotations

from .core import BenchmarkReport


def format_benchmark_report(report: BenchmarkReport) -> str:
    return report.markdown
