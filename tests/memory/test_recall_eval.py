import asyncio

import pytest

from tests.memory.helpers.recall_eval import (
    DeterministicRecallClient,
    RecallEvalCase,
    validate_case,
)


def test_validate_case_rejects_index_entry_without_memory_file():
    case = RecallEvalCase(
        case_id="ghost-index-entry",
        query="remember pandas preferences",
        index_content="- [Ghost](ghost.md) - User prefers pandas\n",
        memory_files={},
        expected_filenames={"ghost.md"},
        expected_top1="ghost.md",
    )

    with pytest.raises(ValueError, match="ghost.md"):
        validate_case(case)


def test_validate_case_rejects_expected_file_missing_from_index():
    case = RecallEvalCase(
        case_id="expected-not-indexed",
        query="remember pandas preferences",
        index_content="- [Other](other.md) - unrelated\n",
        memory_files={
            "other.md": "---\nmetadata:\n  type: user\n---\n\nOther",
            "pandas.md": "---\nmetadata:\n  type: user\n---\n\nPandas",
        },
        expected_filenames={"pandas.md"},
        expected_top1="pandas.md",
    )

    with pytest.raises(ValueError, match="pandas.md"):
        validate_case(case)


def test_deterministic_selector_reads_available_memories_section():
    async def _run():
        client = DeterministicRecallClient()
        response = await client.complete(
            system_prompt="selector",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Query: pandas dataframe analysis\n\n"
                        "Available memories:\n"
                        "- [indexed] pandas-style.md: User prefers pandas dataframes\n"
                        "- [indexed] release-plan.md: Release deadline planning\n"
                    ),
                }
            ],
            max_tokens=256,
        )

        assert response == '["pandas-style.md"]'

    asyncio.run(_run())


def test_deterministic_selector_fails_when_manifest_section_missing():
    async def _run():
        client = DeterministicRecallClient()
        with pytest.raises(ValueError, match="Available memories"):
            await client.complete(
                system_prompt="selector",
                messages=[{"role": "user", "content": "Query: pandas"}],
                max_tokens=256,
            )

    asyncio.run(_run())
