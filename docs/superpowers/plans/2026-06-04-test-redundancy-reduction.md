# Test Redundancy Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce redundant setup and orchestration code inside the highest-value test files while preserving current test behavior, readability, and failure clarity.

**Architecture:** Keep the refactor strictly test-local. Analyze all test files, but only modify the redundancy hotspots already identified in the approved design. Within each target file, introduce small private builders and focused async helpers, then rerun the relevant pytest slices to verify the refactor is behavior-preserving.

**Tech Stack:** Python 3.11, pytest, pytest-asyncio, dataclasses, `types.SimpleNamespace`, `xxcode.config.Config`, `xxcode.agent.state.AgentState`, `xxcode.agent.subagent.SubAgent`, `xxcode.agent.task_runtime.AgentTaskRuntime`

---

## File Structure

- Modify: `tests/memory/test_recall_eval.py`
  Responsibility: shrink inline `RecallEvalCase` construction and one-off async selector wrappers.
- Modify: `tests/memory/test_recall_stability.py`
  Responsibility: remove repeated benchmark-case selection and scorecard setup loops while keeping stability assertions obvious.
- Modify: `tests/memory/test_extraction_eval.py`
  Responsibility: collapse repeated `ExtractionEvalCase` scaffolding into one file-local builder plus a few tiny content helpers.
- Modify: `tests/context/test_context_engineering_eval.py`
  Responsibility: centralize repeated `ContextEvalCase` defaults and scorecard setup without weakening scenario naming.
- Modify: `tests/agent/test_loop_hardening.py`
  Responsibility: deduplicate repeated `Config(...)` and `AgentState(...)` setup in loop-hardening tests.
- Modify: `tests/test_permission_flow.py`
  Responsibility: deduplicate repeated engine/config/state setup while preserving permission-flow readability.
- Modify: `tests/test_task_runtime.py`
  Responsibility: collapse repeated task-record registration boilerplate without abstracting away the runtime orchestration inside async tests.
- Modify: `tests/memory/test_agent_memory.py`
  Responsibility: extract repeated home-patching, subagent setup, and message-text collection helpers for memory-backed subagent tests.
- Reuse without modification: `tests/conftest.py`
  Responsibility: remain untouched in this phase because shared test infrastructure changes are explicitly out of scope.
- Reuse without modification: `tests/memory/helpers/recall_eval.py`
  Responsibility: remain untouched in this phase because the approved scope is file-local deduplication, not shared helper refactoring.

## Task 1: Refactor `test_recall_eval.py` And `test_recall_stability.py`

**Files:**
- Modify: `tests/memory/test_recall_eval.py`
- Modify: `tests/memory/test_recall_stability.py`

- [ ] **Step 1: Rewrite one recall-eval test to use not-yet-defined local helpers**

Change the top of `tests/memory/test_recall_eval.py` so the first case-building
and selector tests call file-local helpers before those helpers exist:

```python
def test_validate_case_rejects_index_entry_without_memory_file():
    case = _make_recall_case(
        case_id="ghost-index-entry",
        index_content="- [Ghost](ghost.md) - User prefers pandas\n",
        expected_filenames={"ghost.md"},
        expected_top1="ghost.md",
    )

    with pytest.raises(ValueError, match="ghost.md"):
        validate_case(case)


@pytest.mark.asyncio
async def test_deterministic_selector_reads_available_memories_section():
    response = await _complete_selector(
        "Query: pandas dataframe analysis\n\n"
        "Available memories:\n"
        "- [indexed] pandas-style.md: User prefers pandas dataframes\n"
        "- [indexed] release-plan.md: Release deadline planning\n"
    )

    assert response == '["pandas-style.md"]'
```

- [ ] **Step 2: Run the focused recall-eval tests to verify they fail first**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py::test_validate_case_rejects_index_entry_without_memory_file tests/memory/test_recall_eval.py::test_deterministic_selector_reads_available_memories_section -v
```

Expected: FAIL with `NameError` for `_make_recall_case` and `_complete_selector`.

- [ ] **Step 3: Add the local builders and convert the remaining one-off async wrappers**

Add these helpers near the imports in `tests/memory/test_recall_eval.py`:

```python
def _make_recall_case(
    *,
    case_id: str,
    query: str = "remember pandas preferences",
    index_content: str = "",
    memory_files: dict[str, str] | None = None,
    expected_filenames: set[str] | None = None,
    expected_top1: str | None = None,
) -> RecallEvalCase:
    return RecallEvalCase(
        case_id=case_id,
        query=query,
        index_content=index_content,
        memory_files=memory_files or {},
        expected_filenames=expected_filenames or set(),
        expected_top1=expected_top1,
    )


async def _complete_selector(user_content: str) -> str:
    client = DeterministicRecallClient()
    return await client.complete(
        system_prompt="selector",
        messages=[{"role": "user", "content": user_content}],
        max_tokens=256,
    )
```

Then refactor the remaining selector tests so they all use `@pytest.mark.asyncio`
plus `await _complete_selector(...)` instead of nested `async def _run(): ...`
followed by `asyncio.run(_run())`.

In `tests/memory/test_recall_stability.py`, add these tiny file-local helpers:

```python
async def _stability_metrics_for(tmp_path, case_index: int):
    case = quality_benchmark_cases()[case_index]
    return await compute_stability_metrics(case, tmp_path / case.case_id)


async def _stability_scorecard_for_all(tmp_path):
    metrics = []
    for case in quality_benchmark_cases():
        metrics.append(await compute_stability_metrics(case, tmp_path / case.case_id))
    return build_stability_scorecard(metrics)
```

Update the async tests to call those helpers instead of open-coding the same
case lookup and metric loop each time.

- [ ] **Step 4: Run the two touched files**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py
git commit -m "test: reduce recall evaluation boilerplate"
```

## Task 2: Refactor `test_extraction_eval.py` And `test_context_engineering_eval.py`

**Files:**
- Modify: `tests/memory/test_extraction_eval.py`
- Modify: `tests/context/test_context_engineering_eval.py`

- [ ] **Step 1: Rewrite one case in each file to use new local builders before defining them**

In `tests/memory/test_extraction_eval.py`, change the first scenario-style
test to start with:

```python
def test_valid_candidate_memory_reports_validity_and_completeness():
    case = _make_extraction_case(
        case_id="valid-candidate",
        conversation=_conversation("I prefer snake_case in Python tests."),
        candidate_memory_files={
            "python-style.md": memory_file(
                "user",
                "Python Style",
                "User prefers snake_case in Python tests",
                "Use snake_case when writing Python tests for the user.",
            ),
        },
        expected_memory_filenames={"python-style.md"},
        expected_facts={"user prefers snake_case python tests"},
        expected_types={"python-style.md": "user"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.case_id == "valid-candidate"
    assert metrics.write_validity_rate == 1.0
```

In `tests/context/test_context_engineering_eval.py`, change `_simple_case()` to:

```python
def _simple_case() -> ContextEvalCase:
    return _make_context_case(
        case_id="constraint-only",
        scenario="Preserve a simple user constraint in the flattened snapshot.",
        cwd_files={"CLAUDE.md": "Always preserve explicit user constraints."},
        messages=[
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "I will inspect the repository."}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "Do not modify settings.py"}],
            },
        ],
        target_turn_index=1,
        expected_present=["Do not modify settings.py"],
        expected_recent_present=["Do not modify settings.py"],
        expected_recall_diagnostics=RecallDiagnostics(
            index_injected=True,
            recalled_count=0,
            recall_empty=True,
        ),
        expected_compression_diagnostics=CompressionDiagnostics(
            compression_used=False,
            level_reached=0,
            summary_injected=False,
        ),
    )
```

- [ ] **Step 2: Run the focused tests to verify they fail first**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py::test_valid_candidate_memory_reports_validity_and_completeness tests/context/test_context_engineering_eval.py::test_run_context_case_returns_snapshot_and_preserves_constraint -v
```

Expected: FAIL with `NameError` for `_make_extraction_case`,
`_conversation`, or `_make_context_case`.

- [ ] **Step 3: Add the extraction-case builders and shared content helpers**

Add these helpers near the top of `tests/memory/test_extraction_eval.py`:

```python
def _conversation(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def _make_extraction_case(
    *,
    case_id: str,
    conversation: list[dict] | None = None,
    existing_memory_files: dict[str, str] | None = None,
    candidate_memory_files: dict[str, str] | None = None,
    expected_memory_filenames: set[str] | None = None,
    expected_facts: set[str] | None = None,
    expected_types: dict[str, str] | None = None,
    source_evidence: dict[str, set[str]] | None = None,
    forbidden_facts: set[str] | None = None,
    duplicate_facts: set[str] | None = None,
    expected_latest_facts: set[str] | None = None,
    obsolete_facts: set[str] | None = None,
    expected_updated_filenames: set[str] | None = None,
    expected_deleted_filenames: set[str] | None = None,
) -> ExtractionEvalCase:
    return ExtractionEvalCase(
        case_id=case_id,
        conversation=conversation or [],
        existing_memory_files=existing_memory_files or {},
        candidate_memory_files=candidate_memory_files or {},
        expected_memory_filenames=expected_memory_filenames or set(),
        expected_facts=expected_facts or set(),
        expected_types=expected_types or {},
        source_evidence=source_evidence or {},
        forbidden_facts=forbidden_facts or set(),
        duplicate_facts=duplicate_facts or set(),
        expected_latest_facts=expected_latest_facts or set(),
        obsolete_facts=obsolete_facts or set(),
        expected_updated_filenames=expected_updated_filenames or set(),
        expected_deleted_filenames=expected_deleted_filenames or set(),
    )


def _existing_snake_case_memory() -> str:
    return memory_file(
        "user",
        "Existing Style",
        "User prefers snake_case",
        "User prefers snake_case in Python tests.",
    )
```

Refactor the repeated duplicate-control and conflict-update scenarios so they
reuse `_make_extraction_case(...)`, `_conversation(...)`, and
`_existing_snake_case_memory()` instead of rebuilding the same dictionaries and
memory payloads inline.

- [ ] **Step 4: Add the context-case builder and a shared default budget constant**

Add these helpers near the top of `tests/context/test_context_engineering_eval.py`:

```python
_DEFAULT_BUDGET = {
    "soft_limit_tokens": 4000,
    "hard_limit_tokens": 8000,
}


def _make_context_case(
    *,
    case_id: str,
    scenario: str,
    messages: list[dict],
    target_turn_index: int,
    cwd_files: dict[str, str] | None = None,
    memory_index_content: str = "",
    memory_files: dict[str, str] | None = None,
    expected_compression_level: int = 0,
    expected_present: list[str] | None = None,
    expected_absent: list[str] | None = None,
    expected_recent_present: list[str] | None = None,
    expected_stale_absent: list[str] | None = None,
    expected_order: list[tuple[str, str]] | None = None,
    required_sections: list[str] | None = None,
    expected_recall_diagnostics: RecallDiagnostics | None = None,
    expected_compression_diagnostics: CompressionDiagnostics | None = None,
    budget_expectation: dict[str, int] | None = None,
) -> ContextEvalCase:
    return ContextEvalCase(
        case_id=case_id,
        scenario=scenario,
        cwd_files=cwd_files or {},
        messages=messages,
        memory_index_content=memory_index_content,
        memory_files=memory_files or {},
        target_turn_index=target_turn_index,
        expected_compression_level=expected_compression_level,
        expected_present=expected_present or [],
        expected_absent=expected_absent or [],
        expected_recent_present=expected_recent_present or [],
        expected_stale_absent=expected_stale_absent or [],
        expected_order=expected_order or [],
        required_sections=required_sections or [],
        expected_recall_diagnostics=expected_recall_diagnostics
        or RecallDiagnostics(index_injected=True, recalled_count=0, recall_empty=True),
        expected_compression_diagnostics=expected_compression_diagnostics
        or CompressionDiagnostics(
            compression_used=False,
            level_reached=expected_compression_level,
            summary_injected=False,
        ),
        budget_expectation=budget_expectation or dict(_DEFAULT_BUDGET),
    )
```

Refactor `_simple_case()`, `_memory_case()`, and `_compressing_case()` to call
this builder so the repeated empty lists, repeated budget dictionaries, and
repeated default diagnostics are no longer copied three times.

- [ ] **Step 5: Run the two touched files**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py tests/context/test_context_engineering_eval.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/memory/test_extraction_eval.py tests/context/test_context_engineering_eval.py
git commit -m "test: reduce evaluation case boilerplate"
```

## Task 3: Refactor `test_loop_hardening.py`

**Files:**
- Modify: `tests/agent/test_loop_hardening.py`

- [ ] **Step 1: Rewrite one async test to use not-yet-defined config and state builders**

Change the first test setup in `tests/agent/test_loop_hardening.py` to:

```python
async def test_output_token_escalation_is_used_for_next_client(tmp_path):
    config = _make_config(tmp_path, api_max_tokens=32)
    engine = CoreExecutionEngine(config)
    client = _TruncateThenFinishClient()
    max_tokens_seen = []

    def _build_client(max_tokens=None, **kwargs):
        max_tokens_seen.append(max_tokens)
        return client

    engine._build_client = _build_client
    state = _make_state("hello")

    events = await _collect_events(engine, state)

    assert [event.type for event in events][-1] == "done"
    assert max_tokens_seen[:2] == [32, ESCALATED_MAX_TOKENS]
```

- [ ] **Step 2: Run the targeted loop-hardening test to verify it fails first**

Run:

```powershell
py -3.11 -m pytest tests/agent/test_loop_hardening.py::test_output_token_escalation_is_used_for_next_client -v
```

Expected: FAIL with `NameError` for `_make_config` or `_make_state`.

- [ ] **Step 3: Add the builders and refactor repeated setup**

Add these helpers near the existing `_collect_events(...)` helper:

```python
def _make_config(tmp_path, **overrides):
    return Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
        **overrides,
    )


def _make_state(text: str, **overrides):
    return AgentState(
        system_prompt="system",
        messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
        **overrides,
    )
```

Refactor the repeated `Config(...)` and `AgentState(...)` blocks throughout the
file to use these builders, but keep scenario-specific overrides inline. For
example:

```python
config = _make_config(tmp_path, max_parent_turns=1)
state = _make_state("loop")
```

and:

```python
config = _make_config(
    tmp_path,
    auto_memory_enabled=True,
    auto_memory_directory=str(memory_dir),
    memory_recall_prefetch_timeout_seconds=0.01,
)
state = _make_state("remember", last_query="remember")
```

- [ ] **Step 4: Run the whole file**

Run:

```powershell
py -3.11 -m pytest tests/agent/test_loop_hardening.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/agent/test_loop_hardening.py
git commit -m "test: deduplicate loop hardening setup"
```

## Task 4: Refactor `test_permission_flow.py`

**Files:**
- Modify: `tests/test_permission_flow.py`

- [ ] **Step 1: Rewrite the first permission test to call not-yet-defined engine helpers**

Change `test_permission_grant_allows_write_tool_to_execute` to start with:

```python
def test_permission_grant_allows_write_tool_to_execute(tmp_path):
    target = tmp_path / "created.txt"
    fake_client = _FakeClient(target)
    engine = _make_engine(tmp_path, fake_client)
    state = _make_state("create file")

    events = asyncio.run(_run_until_done(engine, state))

    assert target.read_text(encoding="utf-8") == "written by test"
    assert any(event.type == "permission_needed" for event in events)
    assert events[-1].type == "done"
```

- [ ] **Step 2: Run the targeted test to verify it fails first**

Run:

```powershell
py -3.11 -m pytest tests/test_permission_flow.py::test_permission_grant_allows_write_tool_to_execute -v
```

Expected: FAIL with `NameError` for `_make_engine` or `_make_state`.

- [ ] **Step 3: Add the engine/config/state builders and refactor repeated setup**

Add these helpers above the first test in `tests/test_permission_flow.py`:

```python
def _make_config(tmp_path, **overrides):
    return Config(
        api_key="test-key",
        api_base_url="https://example.test",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
        **overrides,
    )


def _make_engine(tmp_path, client, **config_overrides):
    engine = CoreExecutionEngine(_make_config(tmp_path, **config_overrides))
    engine._build_client = lambda max_tokens=None: client
    return engine


def _make_state(text: str, **overrides):
    return AgentState(
        system_prompt="system",
        messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
        **overrides,
    )
```

Refactor the repeated engine setup in the file to use `_make_engine(...)` and
the repeated state setup to use `_make_state(...)`. Keep special cases inline,
for example:

```python
engine = _make_engine(tmp_path, fake_client, auto_memory_directory=str(memory_dir))
state = _make_state("answer with preference", last_query="answer with preference")
```

- [ ] **Step 4: Run the whole permission-flow file**

Run:

```powershell
py -3.11 -m pytest tests/test_permission_flow.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_permission_flow.py
git commit -m "test: deduplicate permission flow setup"
```

## Task 5: Refactor `test_task_runtime.py`

**Files:**
- Modify: `tests/test_task_runtime.py`

- [ ] **Step 1: Rewrite the first record-registration tests to use a new local registration helper**

Change the first setup-heavy tests to use `_register_task(...)` before the
helper exists:

```python
def test_wait_for_tasks_returns_completed_snapshot_without_timeout():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="task-1", status="completed")

    result = asyncio.run(
        runtime.wait_for_tasks(["task-1"], scope_id="main", timeout_seconds=30)
    )

    assert result["timeout"] is False
    assert result["pending_task_ids"] == []
    assert result["tasks"][0]["task_id"] == "task-1"
```

- [ ] **Step 2: Run the targeted task-runtime tests to verify they fail first**

Run:

```powershell
py -3.11 -m pytest tests/test_task_runtime.py::test_wait_for_tasks_returns_completed_snapshot_without_timeout tests/test_task_runtime.py::test_wait_for_tasks_soft_timeout_is_not_hard_error -v
```

Expected: FAIL with `NameError` for `_register_task`.

- [ ] **Step 3: Add the registration helper and replace repeated record setup**

Add this helper next to `_record(...)`:

```python
def _register_task(
    runtime: AgentTaskRuntime,
    *,
    task_id: str,
    status: str,
    parent_scope_id: str = "main",
    reusable: bool = False,
) -> AgentTaskRecord:
    record = _record(
        task_id=task_id,
        parent_scope_id=parent_scope_id,
        reusable=reusable,
        status=status,
    )
    runtime._register_record(record, create_task_scope=True)
    return record
```

Refactor the repeated patterns such as:

```python
runtime._register_record(_record(task_id="task-1", status="completed"), create_task_scope=True)
```

into:

```python
_register_task(runtime, task_id="task-1", status="completed")
```

Do the same for repeated foreign-scope, expired-task, and child-scope setups.
Keep the more complex async worker orchestration unchanged except where it
benefits directly from `_register_task(...)`.

- [ ] **Step 4: Run the whole task-runtime file**

Run:

```powershell
py -3.11 -m pytest tests/test_task_runtime.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_task_runtime.py
git commit -m "test: reduce task runtime record boilerplate"
```

## Task 6: Refactor `test_agent_memory.py`

**Files:**
- Modify: `tests/memory/test_agent_memory.py`

- [ ] **Step 1: Rewrite the first subagent test to use new local setup helpers before defining them**

Change `test_subagent_system_prompt_includes_agent_memory` to start with:

```python
def test_subagent_system_prompt_includes_agent_memory(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "explore"

    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        metadata={"type": "reference"},
    ))

    sub = _make_subagent(project, tmp_path)

    prompt = asyncio.run(sub._build_system_prompt())
```

- [ ] **Step 2: Run the targeted subagent test to verify it fails first**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_agent_memory.py::test_subagent_system_prompt_includes_agent_memory -v
```

Expected: FAIL with `NameError` for `_patch_home`, `_make_project_dir`, or `_make_subagent`.

- [ ] **Step 3: Add the home/config/subagent/message-text helpers and refactor repeated setup**

Add these helpers near the top of `tests/memory/test_agent_memory.py`:

```python
def _patch_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _make_project_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


def _make_subagent_config(cwd: Path, tmp_path, **overrides):
    return SimpleNamespace(
        cwd=cwd,
        auto_memory_enabled=True,
        api_model="fake",
        api_key="fake",
        api_base_url="http://fake",
        api_max_tokens=1000,
        max_tool_output_chars=1000,
        session_dir=tmp_path / "sessions",
        **overrides,
    )


def _make_definition(**overrides):
    return SimpleNamespace(
        name="Explore",
        description="Read-only search agent.",
        model=None,
        max_turns=5,
        **overrides,
    )


def _make_subagent(project: Path, tmp_path, *, registry=None, agent_type="Explore", **config_overrides):
    return SubAgent(
        config=_make_subagent_config(project, tmp_path, **config_overrides),
        registry=registry or ToolRegistry(),
        definition=_make_definition(name=agent_type),
        agent_type=agent_type,
    )


def _message_text(messages: list[dict]) -> str:
    return "\n".join(
        block["text"]
        for message in messages
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
```

Then refactor the repeated `SimpleNamespace(...)`, `definition = SimpleNamespace(...)`,
and `SubAgent(...)` blocks throughout the file to use those builders. Replace the
repeated multiline `"\n".join(...)` expressions in the final two tests with:

```python
second_call_text = _message_text(second_call_messages)
```

- [ ] **Step 4: Run the whole agent-memory file and one neighboring memory suite**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_agent_memory.py tests/memory/test_recall.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/test_agent_memory.py
git commit -m "test: reduce agent memory setup duplication"
```

## Task 7: Full Regression, Diff Review, And Final Analysis Summary

**Files:**
- Modify: `tests/memory/test_recall_eval.py`
- Modify: `tests/memory/test_recall_stability.py`
- Modify: `tests/memory/test_extraction_eval.py`
- Modify: `tests/context/test_context_engineering_eval.py`
- Modify: `tests/agent/test_loop_hardening.py`
- Modify: `tests/test_permission_flow.py`
- Modify: `tests/test_task_runtime.py`
- Modify: `tests/memory/test_agent_memory.py`

- [ ] **Step 1: Run the full touched-file regression suite**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py tests/memory/test_extraction_eval.py tests/context/test_context_engineering_eval.py tests/agent/test_loop_hardening.py tests/test_permission_flow.py tests/test_task_runtime.py tests/memory/test_agent_memory.py -v
```

Expected: PASS

- [ ] **Step 2: Run neighboring spot-check suites**

Run:

```powershell
py -3.11 -m pytest tests/context/test_context_engineering_stability.py tests/memory/test_memory_context_eval_report.py tests/test_task_tools.py tests/test_coordinator_integration.py -v
```

Expected: PASS

- [ ] **Step 3: Review the diff and confirm the refactor stayed within scope**

Run:

```powershell
git diff -- tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py tests/memory/test_extraction_eval.py tests/context/test_context_engineering_eval.py tests/agent/test_loop_hardening.py tests/test_permission_flow.py tests/test_task_runtime.py tests/memory/test_agent_memory.py
git status --short -- tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py tests/memory/test_extraction_eval.py tests/context/test_context_engineering_eval.py tests/agent/test_loop_hardening.py tests/test_permission_flow.py tests/test_task_runtime.py tests/memory/test_agent_memory.py
```

Review checklist:

- every new helper is file-local
- `tests/conftest.py` is unchanged
- no new modules were added under `tests/`
- async wrapper reduction did not obscure scenario intent
- parameterization was used only where failure output remains clear
- untouched files remained untouched when abstraction felt forced

- [ ] **Step 4: Write the final analysis summary in the implementation handoff**

The final handoff must include:

```text
- analyzed all test files under tests/
- changed only the approved hotspot files
- list of files changed
- list of files analyzed but intentionally left unchanged
- pytest coverage actually run
- any redundant patterns intentionally deferred for readability or risk reasons
```

- [ ] **Step 5: Commit any final cleanup not already included in earlier commits**

If the diff review required tiny naming or formatting cleanups, commit them with:

```bash
git add tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py tests/memory/test_extraction_eval.py tests/context/test_context_engineering_eval.py tests/agent/test_loop_hardening.py tests/test_permission_flow.py tests/test_task_runtime.py tests/memory/test_agent_memory.py
git commit -m "test: finalize low-risk redundancy cleanup"
```

If there are no post-review changes, skip this commit and leave the earlier task
commits as the full implementation history.

## Plan Self-Review

Spec coverage:

- full-suite analysis is reflected in the file-structure scope and final
  handoff task
- low-risk file-local deduplication is enforced in every task and in the diff
  review checklist
- the approved hotspot files all have explicit implementation tasks
- async-wrapper cleanup is covered in Tasks 1, 3, and 4
- repeated case-construction cleanup is covered in Tasks 1 and 2
- repeated setup-builder cleanup is covered in Tasks 3 through 6
- verification layers from the spec are covered in Task 7 plus the per-task
  file-level runs

Placeholder scan:

- no `TODO`, `TBD`, or “implement later” placeholders remain
- every code-changing step includes concrete code to add or transform
- every verification step includes an exact command and expected result
- no step relies on “same as previous task” shortcuts

Type consistency:

- `RecallEvalCase`, `ExtractionEvalCase`, and `ContextEvalCase` builders use
  keyword-only helper patterns consistently
- `Config`, `AgentState`, and `SubAgent` setup helpers preserve the same core
  constructor shapes used by the current tests
- helper names stay file-local and domain-specific rather than generic across
  the suite
