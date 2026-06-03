from tests.memory.helpers.index_eval import (
    RawIndexEvalCase,
    compute_index_metrics,
    scan_raw_index_links,
)


def test_raw_link_scanner_keeps_duplicates_and_memory_md_references():
    links = scan_raw_index_links(
        "- [Index](MEMORY.md) - should be flagged\n"
        "- [Alpha](alpha.md) - first\n"
        "- [Alpha Again](alpha.md) - duplicate\n"
        "- [Ghost](ghost.md) - stale\n"
    )

    assert [link.filename for link in links] == [
        "MEMORY.md",
        "alpha.md",
        "alpha.md",
        "ghost.md",
    ]
    assert links[0].title == "Index"
    assert links[2].description == "duplicate"


def test_structural_metrics_detect_stale_duplicate_and_memory_md_reference():
    case = RawIndexEvalCase(
        case_id="raw-structural-risk",
        index_content=(
            "- [Index](MEMORY.md) - should be flagged\n"
            "- [Alpha](alpha.md) - first\n"
            "- [Alpha Again](alpha.md) - duplicate\n"
            "- [Ghost](ghost.md) - stale\n"
        ),
        memory_files={
            "alpha.md": _memory_file("user", "Alpha", "First alpha detail"),
        },
        expected_present_filenames={"alpha.md"},
        risk_labels={"memory-md", "duplicate", "stale"},
    )

    metrics = compute_index_metrics(
        case.index_content,
        case.memory_files,
        case_id=case.case_id,
    )

    assert metrics.case_id == "raw-structural-risk"
    assert metrics.indexed_file_count == 2
    assert metrics.memory_file_count == 1
    assert metrics.coverage_rate == 1.0
    assert metrics.stale_reference_rate == 1 / 4
    assert metrics.duplicate_reference_rate == 1 / 4
    assert metrics.parseable_line_rate == 1.0
    assert metrics.memory_md_exclusion == 0.0


def _memory_file(memory_type: str, name: str, description: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {memory_type}\n"
        "---\n\n"
        f"{description}\n"
    )
