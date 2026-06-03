# Context Compression Contributions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic L1-L4 runtime compression contribution accounting and TDD coverage for token, character, and message reduction metrics.

**Architecture:** Extend `CompressionStats` in the runtime pipeline so each compression stage records an explicit token delta while preserving the current compression flow. Add one focused pytest module with deterministic fixtures for isolated L1, L2, L3, L4, and end-to-end telescoping checks, and keep L4 fully offline via monkeypatching.

**Tech Stack:** Python 3.11, pytest, dataclasses, monkeypatch, existing `ContextPipeline` / `microcompact_messages()` / `collapse_messages()` code

---

## File Structure

- Modify: `src/xxcode/context/pipeline.py`
  Responsibility: extend `CompressionStats`, record per-stage token deltas inside `ContextPipeline.compress()`, and make L4 success, suppression, and failure semantics explicit.
- Modify: `src/xxcode/context/micro.py`
  Responsibility: expose an exact helper for counting L2 placeholder-cleared tool results without changing the core microcompact behavior.
- Create: `tests/context/test_compression_contributions.py`
  Responsibility: deterministic TDD coverage for L1 no-op and positive cases, L2 exact block clearing, L3 net message reduction, L4 success/suppress/failure semantics, and end-to-end telescoping equality.
- Reuse without modification: `src/xxcode/context/collapse.py`
  Responsibility: existing collapse behavior already defines `collapse_count` as net message reduction when measured from `len(before) - len(after)`.
- Reuse without modification: `src/xxcode/agent/state.py`
  Responsibility: existing `task_budget_remaining` and `consecutive_autocompact_failures` fields support L4 budget and failure-path tests.

## Task 1: Add L1 Contribution Tests And Stats Fields

**Files:**
- Create: `tests/context/test_compression_contributions.py`
- Modify: `src/xxcode/context/pipeline.py`

- [ ] **Step 1: Write the failing L1 tests and shared test helpers**

Add this initial test module content to `tests/context/test_compression_contributions.py`:

```python
import pytest

import xxcode.context.pipeline as pipeline_module

from xxcode.config import Config
from xxcode.context.pipeline import ContextPipeline


def _make_pipeline(tmp_path):
    config = Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
    )
    return ContextPipeline(config)


def _tool_result_message(text: str, tool_use_id: str = "tool-1") -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": text,
            }
        ],
    }


@pytest.mark.asyncio
async def test_l1_reports_token_and_character_contributions(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    noisy = (
        "Collecting demo-package\n"
        "Downloading demo-package\n"
        "Requirement already satisfied: demo-package\n"
        "Successfully installed demo-package\n\n"
        + ("Collecting demo-package\nDownloading demo-package\n" * 120)
    )

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        [_tool_result_message(noisy)],
        current_tokens=None,
        context_limit=200,
        threshold=0.5,
    )

    assert compressed != [_tool_result_message(noisy)]
    assert stats.snip_removed > 0
    assert stats.snip_tokens_freed > 0
    assert stats.micro_tokens_freed == 0
    assert stats.collapse_tokens_freed == 0
    assert stats.auto_tokens_freed == 0
    assert stats.tokens_before - stats.tokens_after == stats.snip_tokens_freed


@pytest.mark.asyncio
async def test_l1_no_noise_keeps_zero_contribution(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    clean = "plain output " * 400

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    _compressed, stats = await pipeline.compress(
        [_tool_result_message(clean)],
        current_tokens=None,
        context_limit=200,
        threshold=0.2,
    )

    assert stats.snip_removed == 0
    assert stats.snip_tokens_freed == 0
    assert stats.level_reached >= 1
```

- [ ] **Step 2: Run the L1 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l1_reports_token_and_character_contributions tests/context/test_compression_contributions.py::test_l1_no_noise_keeps_zero_contribution -v
```

Expected: FAIL because `CompressionStats` does not yet define `snip_tokens_freed`, `micro_tokens_freed`, `collapse_tokens_freed`, or `auto_tokens_freed`.

- [ ] **Step 3: Add the new stats fields and L1 token accounting**

Update `src/xxcode/context/pipeline.py` like this:

```python
@dataclass
class CompressionStats:
    """Statistics from a compression pass."""

    level_reached: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    snip_tokens_freed: int = 0
    micro_tokens_freed: int = 0
    collapse_tokens_freed: int = 0
    auto_tokens_freed: int = 0
    snip_removed: int = 0
    micro_truncated: int = 0
    micro_cleared: int = 0
    collapse_count: int = 0
    auto_triggered: bool = False
```

Then replace the L1 section of `compress()` with:

```python
        logger.debug("L1 snip: %d tokens -> %d limit", stats.tokens_before, soft_limit)
        before_chars = _total_result_chars(current)
        current = snip_messages(current)
        after_chars = _total_result_chars(current)
        stats.snip_removed = max(0, before_chars - after_chars)
        stats.level_reached = 1

        post_l1_tokens = token_count_with_estimation(current)
        stats.snip_tokens_freed = stats.tokens_before - post_l1_tokens
        stats.tokens_after = post_l1_tokens
        if stats.tokens_after <= soft_limit:
            return current, stats
```

- [ ] **Step 4: Run the L1 tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l1_reports_token_and_character_contributions tests/context/test_compression_contributions.py::test_l1_no_noise_keeps_zero_contribution -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/context/test_compression_contributions.py src/xxcode/context/pipeline.py
git commit -m "Add L1 compression contribution accounting"
```

## Task 2: Add Exact L2 Clearing Metrics

**Files:**
- Modify: `tests/context/test_compression_contributions.py`
- Modify: `src/xxcode/context/micro.py`
- Modify: `src/xxcode/context/pipeline.py`

- [ ] **Step 1: Write the failing L2 tests**

Append this to `tests/context/test_compression_contributions.py`:

```python
def _compressible_round(tool_name: str, tool_use_id: str, text: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": {"path": "/fake/file.txt"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": text,
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_l2_reports_exact_cleared_block_count(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    messages.extend(_compressible_round("read_file", "tool-1", "A" * 1800))
    messages.extend(_compressible_round("run_shell", "tool-2", "B" * 1800))
    messages.extend(_compressible_round("grep_search", "tool-3", "C" * 1800))

    monkeypatch.setattr(pipeline_module, "collapse_messages", lambda current, keep_recent=5: current)
    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=200,
        threshold=0.4,
    )

    assert stats.micro_cleared == 2
    assert stats.micro_tokens_freed > 0
    assert stats.micro_truncated == 2

    preserved_contents = [
        block["content"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "tool_result"
    ]
    assert "C" * 1800 in preserved_contents
```

- [ ] **Step 2: Run the L2 test to verify it fails**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l2_reports_exact_cleared_block_count -v
```

Expected: FAIL because `CompressionStats` does not yet populate `micro_cleared` and `micro_tokens_freed`.

- [ ] **Step 3: Add a placeholder-count helper and wire L2 accounting**

Add this helper to `src/xxcode/context/micro.py`:

```python
def count_cleared_tool_results(messages: list[dict[str, Any]]) -> int:
    """Count tool_result blocks replaced with the microcompact placeholder."""
    count = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result" and block.get("content") == _CLEARED_PLACEHOLDER:
                count += 1
    return count
```

Update the import line in `src/xxcode/context/pipeline.py`:

```python
from .micro import count_cleared_tool_results, microcompact_messages
```

Then replace the L2 section in `compress()` with:

```python
        logger.debug("L2 micro: %d tokens still over limit", stats.tokens_after)
        before_cleared = count_cleared_tool_results(current)
        post_l1_tokens = stats.tokens_after
        current, _edits = microcompact_messages(current, is_cache_cold=True, keep_recent=1)
        after_cleared = count_cleared_tool_results(current)
        stats.micro_cleared = after_cleared - before_cleared
        stats.micro_truncated = stats.micro_cleared
        stats.level_reached = 2

        post_l2_tokens = token_count_with_estimation(current)
        stats.micro_tokens_freed = post_l1_tokens - post_l2_tokens
        stats.tokens_after = post_l2_tokens
        if stats.tokens_after <= soft_limit:
            return current, stats
```

- [ ] **Step 4: Run the L2 test to verify it passes**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l2_reports_exact_cleared_block_count -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/context/test_compression_contributions.py src/xxcode/context/micro.py src/xxcode/context/pipeline.py
git commit -m "Add exact L2 compression contribution metrics; micro_truncated now counts cleared blocks"
```

## Task 3: Add Exact L3 Net Reduction Accounting

**Files:**
- Modify: `tests/context/test_compression_contributions.py`
- Modify: `src/xxcode/context/pipeline.py`

- [ ] **Step 1: Write the failing L3 test**

Append this to `tests/context/test_compression_contributions.py`:

```python
def _exchange(turn_id: int, text_size: int = 500) -> list[dict]:
    text = f"turn-{turn_id}-" + ("x" * text_size)
    return [
        {"role": "user", "content": [{"type": "text", "text": text}]},
        {"role": "assistant", "content": [{"type": "text", "text": text[::-1]}]},
    ]


@pytest.mark.asyncio
async def test_l3_reports_net_message_reduction_and_token_delta(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(8):
        messages.extend(_exchange(turn_id))

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=500,
        threshold=0.4,
    )

    assert stats.collapse_tokens_freed > 0
    # 16 messages total, and with the current role-alternation partitioning
    # each message forms its own exchange. keep_recent=5 preserves the newest
    # 5 messages; the older 11 messages collapse to 1 summary.
    # Net reduction = 16 - (1 + 5) = 10.
    assert stats.collapse_count == 10
    collapsed_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]
    assert any(text.startswith("[Earlier conversation") for text in collapsed_texts)
```

- [ ] **Step 2: Run the L3 test to verify it fails**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l3_reports_net_message_reduction_and_token_delta -v
```

Expected: FAIL because `collapse_tokens_freed` is not yet populated.

- [ ] **Step 3: Add L3 token accounting**

Replace the L3 section in `src/xxcode/context/pipeline.py` with:

```python
        logger.debug("L3 collapse: %d tokens still over limit", stats.tokens_after)
        post_l2_tokens = stats.tokens_after
        before_msgs = len(current)
        current = collapse_messages(current, keep_recent=5)
        stats.collapse_count = max(0, before_msgs - len(current))
        stats.level_reached = 3

        post_l3_tokens = token_count_with_estimation(current)
        stats.collapse_tokens_freed = post_l2_tokens - post_l3_tokens
        stats.tokens_after = post_l3_tokens
        if stats.tokens_after <= soft_limit:
            return current, stats
```

- [ ] **Step 4: Run the L3 test to verify it passes**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l3_reports_net_message_reduction_and_token_delta -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/context/test_compression_contributions.py src/xxcode/context/pipeline.py
git commit -m "Add L3 compression contribution accounting"
```

## Task 4: Add Deterministic L4 Success Accounting

**Files:**
- Modify: `tests/context/test_compression_contributions.py`
- Modify: `src/xxcode/context/pipeline.py`

- [ ] **Step 1: Write the failing L4 success-path test**

Append this to `tests/context/test_compression_contributions.py`:

```python
from xxcode.agent.state import AgentState
from xxcode.context.tokens import token_count_with_estimation


@pytest.mark.asyncio
async def test_l4_success_reports_budget_and_token_delta(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    seen: dict[str, int] = {}

    async def _fake_autocompact(self, current, system_prompt):
        seen["post_l3_tokens"] = token_count_with_estimation(current)
        return "condensed summary"

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fake_autocompact)

    state = AgentState(system_prompt="system")
    state.task_budget_remaining = 50_000

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
        state=state,
    )

    summary_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]

    assert stats.auto_triggered is True
    assert stats.auto_tokens_freed > 0
    assert any("[Conversation summary]" in text for text in summary_texts)
    assert state.task_budget_remaining == 50_000 - seen["post_l3_tokens"]
```

- [ ] **Step 2: Run the L4 success-path test to verify it fails**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l4_success_reports_budget_and_token_delta -v
```

Expected: FAIL because `auto_tokens_freed` is not yet populated.

- [ ] **Step 3: Add explicit L4 success accounting**

Replace the L4 section in `src/xxcode/context/pipeline.py` with:

```python
        post_l3_tokens = stats.tokens_after
        failure_count = (
            int(state.consecutive_autocompact_failures)
            if state is not None and hasattr(state, "consecutive_autocompact_failures")
            else self._consecutive_autocompact_failures
        )
        l1_char_tokens_freed = stats.snip_removed // 4
        if not should_autocompact(
            current_tokens=stats.tokens_after,
            snip_tokens_freed=l1_char_tokens_freed,
            context_limit=context_limit,
            consecutive_failures=failure_count,
        ):
            logger.debug(
                "L4 suppressed: tokens_after=%d, snip_freed=%d, consecutive_failures=%d",
                stats.tokens_after, l1_char_tokens_freed, failure_count,
            )
            stats.auto_tokens_freed = 0
            stats.tokens_after = token_count_with_estimation(current)
            return current, stats

        logger.debug("L4 auto: nuclear option triggered")

        if state is not None and getattr(state, "task_budget_remaining", None) is not None:
            final_tokens_before_nuke = token_count_with_estimation(current)
            state.task_budget_remaining -= final_tokens_before_nuke
            logger.debug(
                "L4 budget carryover: deducted %d tokens, %d remaining",
                final_tokens_before_nuke, state.task_budget_remaining,
            )

        stats.auto_triggered = True
        stats.level_reached = 4

        try:
            summary = await self._autocompact(current, system_prompt)
            current = _inject_summary(current, summary, keep_recent=2)
            self._consecutive_autocompact_failures = 0
            if state is not None and hasattr(state, "consecutive_autocompact_failures"):
                state.consecutive_autocompact_failures = 0
        except Exception as e:
            logger.warning("L4 autocompact failed: %s", e)
            if state is not None and hasattr(state, "consecutive_autocompact_failures"):
                state.consecutive_autocompact_failures += 1
                self._consecutive_autocompact_failures = state.consecutive_autocompact_failures
            else:
                self._consecutive_autocompact_failures += 1

        final_tokens = token_count_with_estimation(current)
        stats.auto_tokens_freed = post_l3_tokens - final_tokens
        stats.tokens_after = final_tokens
        return current, stats
```

- [ ] **Step 4: Run the L4 success-path test to verify it passes**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l4_success_reports_budget_and_token_delta -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/context/test_compression_contributions.py src/xxcode/context/pipeline.py
git commit -m "Add L4 success contribution accounting"
```

## Task 5: Add L4 Suppression And Attempted-Failure Coverage

**Files:**
- Modify: `tests/context/test_compression_contributions.py`
- Modify: `src/xxcode/context/pipeline.py`

- [ ] **Step 1: Write the failing L4 suppression and failure-path tests**

Append this to `tests/context/test_compression_contributions.py`:

```python
@pytest.mark.asyncio
async def test_l4_suppressed_keeps_zero_auto_contribution(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
    )

    summary_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]

    assert stats.auto_triggered is False
    assert stats.auto_tokens_freed == 0
    assert all("[Conversation summary]" not in text for text in summary_texts)


@pytest.mark.asyncio
async def test_l4_failure_still_marks_attempt_but_no_token_gain(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    async def _fail_autocompact(self, current, system_prompt):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fail_autocompact)

    state = AgentState(system_prompt="system")
    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
        state=state,
    )

    summary_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]

    assert stats.auto_triggered is True
    assert stats.auto_tokens_freed == 0
    assert all("[Conversation summary]" not in text for text in summary_texts)
    assert state.consecutive_autocompact_failures == 1
```

- [ ] **Step 2: Run the L4 suppression and failure tests to verify the Task 4 code already satisfies them**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l4_suppressed_keeps_zero_auto_contribution tests/context/test_compression_contributions.py::test_l4_failure_still_marks_attempt_but_no_token_gain -v
```

Expected: PASS because Task 4 already gives the correct suppression and failed-attempt behavior; this step confirms the semantics before deciding whether any readability-only cleanup is still needed.

- [ ] **Step 3: If needed, make the L4 zero-contribution semantics more explicit**

If Step 2 already passes and the code is readable enough, skip the edit and go
directly to Step 4. If the code still feels ambiguous, adjust
`src/xxcode/context/pipeline.py` so the attempted/suppressed semantics are
obvious in code:

```python
        if not should_autocompact(
            current_tokens=stats.tokens_after,
            snip_tokens_freed=l1_char_tokens_freed,
            context_limit=context_limit,
            consecutive_failures=failure_count,
        ):
            logger.debug(
                "L4 suppressed: tokens_after=%d, snip_freed=%d, consecutive_failures=%d",
                stats.tokens_after, l1_char_tokens_freed, failure_count,
            )
            stats.auto_triggered = False
            stats.auto_tokens_freed = 0
            stats.tokens_after = token_count_with_estimation(current)
            return current, stats

        stats.auto_triggered = True
        stats.level_reached = 4
```

Keep the `final_tokens` section from Task 4 unchanged so failed attempts naturally leave:

```python
        final_tokens = token_count_with_estimation(current)
        stats.auto_tokens_freed = post_l3_tokens - final_tokens
        stats.tokens_after = final_tokens
```

- [ ] **Step 4: Run the L4 suppression and failure tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_l4_suppressed_keeps_zero_auto_contribution tests/context/test_compression_contributions.py::test_l4_failure_still_marks_attempt_but_no_token_gain -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/context/test_compression_contributions.py src/xxcode/context/pipeline.py
git commit -m "Clarify L4 suppression and failure contribution semantics"
```

## Task 6: Add End-To-End Telescoping Regression

**Files:**
- Modify: `tests/context/test_compression_contributions.py`
- Modify: `src/xxcode/context/pipeline.py`

- [ ] **Step 1: Write the failing end-to-end regression**

Append this to `tests/context/test_compression_contributions.py`:

```python
@pytest.mark.asyncio
async def test_end_to_end_contributions_telescope_to_total_delta(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    messages.append(
        _tool_result_message(
            "Collecting demo\nDownloading demo\nSuccessfully installed demo\n" + ("x" * 1200),
            tool_use_id="noise-1",
        )
    )
    messages.extend(_compressible_round("read_file", "tool-1", "A" * 1800))
    messages.extend(_compressible_round("run_shell", "tool-2", "B" * 1800))
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    async def _fake_autocompact(self, current, system_prompt):
        return "condensed summary"

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fake_autocompact)

    _compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
    )

    per_level = (
        stats.snip_tokens_freed
        + stats.micro_tokens_freed
        + stats.collapse_tokens_freed
        + stats.auto_tokens_freed
    )

    positive_levels = sum(
        value > 0
        for value in (
            stats.snip_tokens_freed,
            stats.micro_tokens_freed,
            stats.collapse_tokens_freed,
            stats.auto_tokens_freed,
        )
    )

    assert stats.tokens_before > stats.tokens_after
    assert positive_levels >= 2
    assert per_level == stats.tokens_before - stats.tokens_after
```

- [ ] **Step 2: Run the end-to-end regression to verify it fails**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_end_to_end_contributions_telescope_to_total_delta -v
```

Expected: FAIL if any stage still reuses stale `stats.tokens_after` values or leaves an uninitialized contribution field.

- [ ] **Step 3: Verify the explicit stage token variables already support telescoping equality**

If Tasks 1 through 5 were applied exactly as written, `compress()` should
already use explicit neighboring estimates. Verify that
`src/xxcode/context/pipeline.py` still contains these stage-boundary
relationships:

```python
        post_l1_tokens = token_count_with_estimation(current)
        stats.snip_tokens_freed = stats.tokens_before - post_l1_tokens
        stats.tokens_after = post_l1_tokens

        post_l2_tokens = token_count_with_estimation(current)
        stats.micro_tokens_freed = post_l1_tokens - post_l2_tokens
        stats.tokens_after = post_l2_tokens

        post_l3_tokens = token_count_with_estimation(current)
        stats.collapse_tokens_freed = post_l2_tokens - post_l3_tokens
        stats.tokens_after = post_l3_tokens

        final_tokens = token_count_with_estimation(current)
        stats.auto_tokens_freed = post_l3_tokens - final_tokens
        stats.tokens_after = final_tokens
```

Do not collapse these variables back into anonymous inline expressions. If an
earlier task drifted from this layout, make the smallest code edit needed to
restore these explicit stage boundaries before rerunning the regression. If the
verification already passes exactly as-is, skip the code edit and proceed
directly to Step 4.

- [ ] **Step 4: Run the end-to-end regression to verify it passes**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py::test_end_to_end_contributions_telescope_to_total_delta -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/context/test_compression_contributions.py src/xxcode/context/pipeline.py
git commit -m "Add end-to-end compression contribution regression"
```

## Task 7: Final Verification

**Files:**
- Modify: `tests/context/test_compression_contributions.py`
- Modify: `src/xxcode/context/pipeline.py`
- Modify: `src/xxcode/context/micro.py`

- [ ] **Step 1: Run the focused context contribution suite**

Run:

```powershell
py -3.11 -m pytest tests/context/test_compression_contributions.py tests/context/test_pipeline_hardening.py tests/context/test_tokens.py -v
```

Expected: PASS

- [ ] **Step 2: Run broader context regressions**

Run:

```powershell
py -3.11 -m pytest tests/context tests/agent/test_query_engine_stages.py tests/test_permission_flow.py -v
```

Expected: PASS
Confirmed at plan-writing time: `tests/test_permission_flow.py` exists in the repository.

- [ ] **Step 3: Compile the source tree**

Run:

```powershell
py -3.11 -m compileall -q src/xxcode
```

Expected: exit code 0

- [ ] **Step 4: Review the diff**

Run:

```powershell
git diff -- src/xxcode/context/pipeline.py src/xxcode/context/micro.py tests/context/test_compression_contributions.py
git status --short -- src/xxcode/context/pipeline.py src/xxcode/context/micro.py tests/context/test_compression_contributions.py
```

Review checklist:

- `CompressionStats` exposes all per-level token fields plus `micro_cleared`
- L1 no-op fixtures keep `snip_tokens_freed == 0`
- L2 exact block clearing uses `micro_cleared`
- `collapse_count` remains net message reduction
- L4 success, suppression, and failure semantics are all deterministic
- telescoping equality holds in the end-to-end regression

## Plan Self-Review

Spec coverage:

- Per-level token contribution fields are covered by Tasks 1 through 4.
- `micro_cleared` exactness is covered by Task 2.
- `collapse_count` net reduction semantics are covered by Task 3.
- L4 success, suppression, failure semantics, and budget carryover are covered by Tasks 4 and 5.
- Telescoping equality is covered by Task 6.
- Focused and broader verification are covered by Task 7.

Placeholder scan:

- No `TODO`, `TBD`, or "implement later" placeholders remain.
- Each test-writing step includes concrete code and an exact pytest command.
- Each implementation step includes concrete code snippets rather than abstract instructions.

Type consistency:

- `CompressionStats` uses `snip_tokens_freed`, `micro_tokens_freed`, `collapse_tokens_freed`, `auto_tokens_freed`, and `micro_cleared` consistently across all tasks.
- The L4 tests consistently treat `auto_triggered` as "attempted" and `auto_tokens_freed` as the actual delta.
- The plan consistently uses `current_tokens=None` for pipeline-computed baselines unless a task explicitly says otherwise.
