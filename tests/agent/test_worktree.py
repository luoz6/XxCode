"""Tests for git worktree isolation manager."""

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from xxcode.agent.worktree import WorktreeManager, WorktreeResult


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        cwd=cwd,
    )


def _canonical_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _listed_worktree_paths(repo: Path) -> set[str]:
    list_proc = _git(["worktree", "list", "--porcelain"], cwd=repo)
    return {
        _canonical_path(line.removeprefix("worktree "))
        for line in list_proc.stdout.splitlines()
        if line.startswith("worktree ")
    }


@pytest.fixture
def git_repo():
    """Create a real git repo in a temp directory with an initial commit."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        _git(["init", "-b", "main"], cwd=root)
        (root / "README.md").write_text("# Test")
        _git(["config", "user.email", "test@test.com"], cwd=root)
        _git(["config", "user.name", "Test"], cwd=root)
        _git(["add", "."], cwd=root)
        _git(["commit", "-m", "init"], cwd=root)
        yield root


@pytest.fixture
def git_repo_with_subdir(git_repo):
    subdir = git_repo / "sub" / "deep"
    subdir.mkdir(parents=True)
    return subdir


class TestFindGitRoot:
    def test_repo_root(self, git_repo):
        result = WorktreeManager.find_git_root(git_repo)
        assert result is not None
        assert result.resolve() == git_repo.resolve()

    def test_subdir(self, git_repo_with_subdir, git_repo):
        result = WorktreeManager.find_git_root(git_repo_with_subdir)
        assert result is not None
        assert result.resolve() == git_repo.resolve()

    def test_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = WorktreeManager.find_git_root(Path(tmp))
            assert result is None


class TestWorktreeCreateAndRemove:
    def test_create_and_remove(self, git_repo):
        async def _run():
            result = await WorktreeManager.create(git_repo, base_ref="HEAD")
            assert result.repo_root == git_repo
            assert result.worktree_path is not None
            assert result.worktree_path.exists()
            assert (result.worktree_path / "README.md").exists()
            assert (result.worktree_path / "README.md").read_text() == "# Test"

            # Verify it appears in git worktree list. Canonicalize paths so
            # Windows short names (RUNNER~1) and long names compare equal.
            wt_path_normalized = _canonical_path(result.worktree_path)
            assert wt_path_normalized in _listed_worktree_paths(git_repo)

            await WorktreeManager.remove(result.worktree_path)
            assert not result.worktree_path.exists()

            # Verify it's gone from git worktree list
            assert wt_path_normalized not in _listed_worktree_paths(git_repo)

        asyncio.run(_run())

    def test_remove_is_idempotent(self, git_repo):
        async def _run():
            result = await WorktreeManager.create(git_repo)
            assert result.worktree_path is not None
            await WorktreeManager.remove(result.worktree_path)
            # Second call must not raise
            await WorktreeManager.remove(result.worktree_path)
            # Third call on None must not raise
            await WorktreeManager.remove(None)

        asyncio.run(_run())

    def test_remove_nonexistent_path(self):
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                nonexistent = Path(tmp) / "does-not-exist"
                await WorktreeManager.remove(nonexistent)

        asyncio.run(_run())

    def test_degraded_when_not_git_repo(self):
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                not_a_repo = Path(tmp) / "not-a-repo"
                not_a_repo.mkdir()
                result = await WorktreeManager.create(not_a_repo)
                assert result.repo_root == not_a_repo
                assert result.worktree_path is None

        asyncio.run(_run())

    def test_worktree_isolation_independent_filesystems(self, git_repo):
        """Two worktrees from the same repo are independent file systems."""
        async def _run():
            r1 = await WorktreeManager.create(git_repo, agent_type="agent-a")
            r2 = await WorktreeManager.create(git_repo, agent_type="agent-b")
            assert r1.worktree_path is not None
            assert r2.worktree_path is not None

            try:
                # Agent A creates a file
                (r1.worktree_path / "from_a.txt").write_text("hello from A")
                # Agent B creates a file
                (r2.worktree_path / "from_b.txt").write_text("hello from B")

                # A cannot see B's file
                assert not (r1.worktree_path / "from_b.txt").exists()
                # B cannot see A's file
                assert not (r2.worktree_path / "from_a.txt").exists()
                # Parent repo cannot see either
                assert not (git_repo / "from_a.txt").exists()
                assert not (git_repo / "from_b.txt").exists()
            finally:
                await WorktreeManager.remove(r1.worktree_path)
                await WorktreeManager.remove(r2.worktree_path)

        asyncio.run(_run())


class TestAgentToolWorktreeSync:
    """Verify worktree isolation in AgentTool.execute sync path."""

    def test_sync_agent_creates_and_cleans_up_worktree(self, git_repo):
        from xxcode.config import Config
        from xxcode.tools.agent.tool import AgentInput, AgentTool

        config = Config(cwd=git_repo, worktree_base_ref="HEAD")

        async def _run():
            tool = AgentTool()

            class _FakeTaskRuntime:
                def __init__(self):
                    self.foreground_tasks = {}
                    self.scopes = set()
                    self.cleaned_scopes = []
                    self.completed_tasks = []

                def register_foreground_task(self, **kwargs):
                    self.foreground_tasks[kwargs["task_id"]] = kwargs
                    return kwargs

                def complete_foreground_task(self, record, termination_reason):
                    self.completed_tasks.append(record["task_id"])

                def fail_foreground_task(self, record, termination_reason):
                    pass

                def discard_foreground_task(self, task_id):
                    self.foreground_tasks.pop(task_id, None)

                def ensure_scope(self, scope_id):
                    self.scopes.add(scope_id)

                async def cleanup_scope(self, scope_id):
                    self.cleaned_scopes.append(scope_id)
                    from xxcode.agent.task_runtime import ScopeCleanupReport
                    return ScopeCleanupReport()

                def list_tasks(self, scope_id):
                    return []

            runtime = _FakeTaskRuntime()

            class _FakeRegistry:
                def list_tools(self):
                    return ["read_file", "grep_search", "glob_match"]

                def get_api_schemas(self):
                    return []

                def get(self, name):
                    return None

            input_data = AgentInput(
                description="test worktree isolation",
                prompt="list files",
                subagent_type="Explore",
                isolation="worktree",
            )

            context = {
                "config": config,
                "_registry": _FakeRegistry(),
                "task_runtime": runtime,
                "parent_state": None,
                "scope_id": "main",
                "cwd": str(git_repo),
            }

            # SubAgent will fail without real API config, but worktree
            # creation and cleanup should still happen.
            try:
                await tool.execute(input_data, context)
            except Exception:
                pass

            # Worktree directories must be cleaned up even on failure.
            worktrees_dir = git_repo / ".xxcode" / "worktrees"
            if worktrees_dir.exists():
                remaining = list(worktrees_dir.iterdir())
                assert len(remaining) == 0, f"Worktree not cleaned up: {remaining}"

        asyncio.run(_run())

    def test_sync_agent_degraded_when_not_git_repo(self):
        import tempfile
        from xxcode.config import Config
        from xxcode.tools.agent.tool import AgentInput, AgentTool

        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                not_a_repo = Path(tmp) / "not-a-repo"
                not_a_repo.mkdir()
                config = Config(cwd=not_a_repo)

                tool = AgentTool()
                input_data = AgentInput(
                    description="test",
                    prompt="test",
                    isolation="worktree",
                )

                class _FakeRuntime:
                    def list_tasks(self, scope_id):
                        return []

                    def register_foreground_task(self, **kwargs):
                        return kwargs

                    def complete_foreground_task(self, record, reason):
                        pass

                    def fail_foreground_task(self, record, reason):
                        pass

                    def discard_foreground_task(self, task_id):
                        pass

                    def ensure_scope(self, scope_id):
                        pass

                    async def cleanup_scope(self, scope_id):
                        from xxcode.agent.task_runtime import ScopeCleanupReport
                        return ScopeCleanupReport()

                class _FakeRegistry:
                    def list_tools(self):
                        return ["read_file"]

                    def get_api_schemas(self):
                        return []

                    def get(self, name):
                        return None

                context = {
                    "config": config,
                    "_registry": _FakeRegistry(),
                    "task_runtime": _FakeRuntime(),
                    "parent_state": None,
                    "scope_id": "main",
                    "cwd": str(not_a_repo),
                }

                try:
                    await tool.execute(input_data, context)
                except Exception:
                    pass

                # No worktree directory should be created in non-git repo
                worktrees_dir = not_a_repo / ".xxcode" / "worktrees"
                assert not worktrees_dir.exists()

        asyncio.run(_run())


class TestWorkerSessionWorktreeCleanup:
    """Verify worktree cleanup via WorkerSession lifecycle hooks."""

    def test_finalize_worker_session_cleans_up_worktree(self, git_repo):
        """_finalize_worker_session must remove the worker's worktree."""
        from unittest.mock import MagicMock

        from xxcode.agent.task_runtime import (
            AgentTaskRecord,
            AgentTaskRuntime,
            WorkerSession,
        )

        async def _run():
            wt_result = await WorktreeManager.create(git_repo, agent_type="test-agent")
            assert wt_result.worktree_path is not None
            wt_path = wt_result.worktree_path

            runtime = AgentTaskRuntime()
            task_id = "subagent-test-deadbeef"
            now = 0.0

            record = AgentTaskRecord(
                task_id=task_id,
                parent_task_id=None,
                parent_scope_id="main",
                worker_label="test-worker",
                description="test",
                agent_type="general-purpose",
                reusable=False,
                status="completed",
                created_at=now,
                updated_at=now,
            )
            runtime._records[task_id] = record
            runtime._task_parent_scopes[task_id] = "main"

            mock_subagent = MagicMock()
            mock_session_state = MagicMock()
            mock_session_state.abort_check = lambda: False

            worker = WorkerSession(
                runtime=runtime,
                record=record,
                subagent=mock_subagent,
                session_state=mock_session_state,
                worktree_path=wt_path,
            )
            runtime._workers[task_id] = worker

            await runtime._finalize_worker_session(worker)

            assert task_id not in runtime._workers
            assert not wt_path.exists()

        asyncio.run(_run())

    def test_finalize_worker_session_idempotent_on_missing_worktree(self, git_repo):
        """_finalize_worker_session must not raise if worktree is already gone."""
        from unittest.mock import MagicMock

        from xxcode.agent.task_runtime import (
            AgentTaskRecord,
            AgentTaskRuntime,
            WorkerSession,
        )

        async def _run():
            wt_result = await WorktreeManager.create(git_repo, agent_type="test-agent")
            assert wt_result.worktree_path is not None
            wt_path = wt_result.worktree_path

            runtime = AgentTaskRuntime()
            task_id = "subagent-test-cafebabe"
            now = 0.0

            record = AgentTaskRecord(
                task_id=task_id,
                parent_task_id=None,
                parent_scope_id="main",
                worker_label="test-worker",
                description="test",
                agent_type="general-purpose",
                reusable=False,
                status="completed",
                created_at=now,
                updated_at=now,
            )
            runtime._records[task_id] = record
            runtime._task_parent_scopes[task_id] = "main"

            mock_subagent = MagicMock()
            mock_session_state = MagicMock()
            mock_session_state.abort_check = lambda: False

            worker = WorkerSession(
                runtime=runtime,
                record=record,
                subagent=mock_subagent,
                session_state=mock_session_state,
                worktree_path=wt_path,
            )
            runtime._workers[task_id] = worker

            # Remove worktree manually first.
            await WorktreeManager.remove(wt_path)
            assert not wt_path.exists()

            # _finalize_worker_session must not raise.
            await runtime._finalize_worker_session(worker)
            assert task_id not in runtime._workers

        asyncio.run(_run())

    def test_cleanup_scope_cleans_up_worktree(self, git_repo):
        """cleanup_scope must clean up worker worktrees (belt-and-suspenders)."""
        from unittest.mock import MagicMock

        from xxcode.agent.task_runtime import (
            AgentTaskRecord,
            AgentTaskRuntime,
            WorkerSession,
        )

        async def _run():
            wt_result = await WorktreeManager.create(git_repo, agent_type="test-agent")
            assert wt_result.worktree_path is not None
            wt_path = wt_result.worktree_path

            runtime = AgentTaskRuntime()
            task_id = "subagent-test-feed1234"
            now = 0.0

            record = AgentTaskRecord(
                task_id=task_id,
                parent_task_id=None,
                parent_scope_id="main",
                worker_label="test-worker",
                description="test",
                agent_type="general-purpose",
                reusable=False,
                status="killed",
                created_at=now,
                updated_at=now,
            )
            runtime._records[task_id] = record
            runtime._task_parent_scopes[task_id] = "main"
            runtime._child_counts["main"] = runtime._child_counts.get("main", 0) + 1

            mock_subagent = MagicMock()
            mock_session_state = MagicMock()
            mock_session_state.abort_check = lambda: False

            worker = WorkerSession(
                runtime=runtime,
                record=record,
                subagent=mock_subagent,
                session_state=mock_session_state,
                worktree_path=wt_path,
            )
            runtime._workers[task_id] = worker
            runtime.ensure_scope(task_id)

            # cleanup_scope on "main" finds records with parent_scope_id="main".
            await runtime.cleanup_scope("main")

            assert not wt_path.exists()

        asyncio.run(_run())

    def test_finalize_worker_session_no_worktree_is_noop(self):
        """_finalize_worker_session with worktree_path=None must not raise."""
        from unittest.mock import MagicMock

        from xxcode.agent.task_runtime import (
            AgentTaskRecord,
            AgentTaskRuntime,
            WorkerSession,
        )

        async def _run():
            runtime = AgentTaskRuntime()
            task_id = "subagent-test-nodeadbeef"
            now = 0.0

            record = AgentTaskRecord(
                task_id=task_id,
                parent_task_id=None,
                parent_scope_id="main",
                worker_label="test-worker",
                description="test",
                agent_type="general-purpose",
                reusable=False,
                status="completed",
                created_at=now,
                updated_at=now,
            )
            runtime._records[task_id] = record
            runtime._task_parent_scopes[task_id] = "main"

            mock_subagent = MagicMock()
            mock_session_state = MagicMock()
            mock_session_state.abort_check = lambda: False

            worker = WorkerSession(
                runtime=runtime,
                record=record,
                subagent=mock_subagent,
                session_state=mock_session_state,
                worktree_path=None,
            )
            runtime._workers[task_id] = worker

            await runtime._finalize_worker_session(worker)
            assert task_id not in runtime._workers

        asyncio.run(_run())


class TestAgentToolBackgroundWorktree:
    """Verify AgentTool background path with worktree isolation."""

    def test_background_agent_creates_worktree_and_passes_to_spawn(self, git_repo):
        """Background worker with isolation=worktree creates worktree and passes it."""
        from xxcode.config import Config
        from xxcode.tools.agent.tool import AgentInput, AgentTool

        config = Config(cwd=git_repo, worktree_base_ref="HEAD")

        async def _run():
            tool = AgentTool()

            spawn_calls = []

            class _FakeTaskRuntime:
                def __init__(self):
                    self.scopes = set()

                def list_tasks(self, scope_id):
                    return []

                def ensure_scope(self, scope_id):
                    self.scopes.add(scope_id)

                async def spawn_worker(self, **kwargs):
                    spawn_calls.append(kwargs)
                    import time
                    from xxcode.agent.task_runtime import AgentTaskRecord
                    return AgentTaskRecord(
                        task_id="subagent-test-bg12345678",
                        parent_task_id=None,
                        parent_scope_id="main",
                        worker_label=kwargs.get("worker_label", "test"),
                        description=kwargs.get("description", ""),
                        agent_type=kwargs.get("agent_type", "general-purpose"),
                        reusable=kwargs.get("reusable", False),
                        status="queued",
                        created_at=time.time(),
                        updated_at=time.time(),
                    )

            from unittest.mock import MagicMock

            def _make_tool(name):
                t = MagicMock()
                t.name = name
                t._should_defer = False
                t.is_read_only.return_value = True
                t.aliases = []
                t.deprecated_aliases = {}
                return t

            class _FakeRegistry:
                def list_tools(self):
                    return [_make_tool("read_file"), _make_tool("grep_search"), _make_tool("glob_match")]

                def get_api_schemas(self):
                    return []

                def get(self, name):
                    return None

            input_data = AgentInput(
                description="test background worktree",
                prompt="do something",
                subagent_type="Explore",
                isolation="worktree",
                run_in_background=True,
            )

            context = {
                "config": config,
                "_registry": _FakeRegistry(),
                "task_runtime": _FakeTaskRuntime(),
                "parent_state": None,
                "scope_id": "main",
                "cwd": str(git_repo),
            }

            result = await tool.execute(input_data, context)

            assert "Background worker launched" in result
            assert len(spawn_calls) == 1
            call = spawn_calls[0]
            assert call.get("worktree_path") is not None
            wt_path = Path(call["worktree_path"])
            assert wt_path.exists()
            assert call["extra_context"].get("worktree_cwd") == str(wt_path)

            # Clean up the worktree manually since we mocked spawn_worker.
            await WorktreeManager.remove(wt_path)
            assert not wt_path.exists()

        asyncio.run(_run())

    def test_background_agent_degraded_when_not_git_repo(self):
        """Background worker with isolation=worktree in non-git repo degrades gracefully."""
        import tempfile

        from xxcode.config import Config
        from xxcode.tools.agent.tool import AgentInput, AgentTool

        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                not_a_repo = Path(tmp) / "not-a-repo"
                not_a_repo.mkdir()
                config = Config(cwd=not_a_repo)

                tool = AgentTool()
                spawn_calls = []

                class _FakeTaskRuntime:
                    def list_tasks(self, scope_id):
                        return []

                    def ensure_scope(self, scope_id):
                        pass

                    async def spawn_worker(self, **kwargs):
                        spawn_calls.append(kwargs)
                        import time
                        from xxcode.agent.task_runtime import AgentTaskRecord
                        return AgentTaskRecord(
                            task_id="subagent-test-bg99999999",
                            parent_task_id=None,
                            parent_scope_id="main",
                            worker_label=kwargs.get("worker_label", "test"),
                            description=kwargs.get("description", ""),
                            agent_type=kwargs.get("agent_type", "general-purpose"),
                            reusable=False,
                            status="queued",
                            created_at=time.time(),
                            updated_at=time.time(),
                        )

                from unittest.mock import MagicMock

                def _make_tool(name):
                    t = MagicMock()
                    t.name = name
                    t._should_defer = False
                    t.is_read_only.return_value = False
                    t.aliases = []
                    t.deprecated_aliases = {}
                    return t

                class _FakeRegistry:
                    def list_tools(self):
                        return [_make_tool("read_file")]

                    def get_api_schemas(self):
                        return []

                    def get(self, name):
                        return None

                input_data = AgentInput(
                    description="test bg degraded",
                    prompt="test",
                    isolation="worktree",
                    run_in_background=True,
                )

                context = {
                    "config": config,
                    "_registry": _FakeRegistry(),
                    "task_runtime": _FakeTaskRuntime(),
                    "parent_state": None,
                    "scope_id": "main",
                    "cwd": str(not_a_repo),
                }

                result = await tool.execute(input_data, context)
                assert "Background worker launched" in result
                assert len(spawn_calls) == 1
                assert spawn_calls[0].get("worktree_path") is None

                worktrees_dir = not_a_repo / ".xxcode" / "worktrees"
                assert not worktrees_dir.exists()

        asyncio.run(_run())


class TestMultiWorkerWorktreeIsolation:
    """Verify multiple worktree workers are filesystem-isolated from each other."""

    def test_parallel_workers_independent_filesystems(self, git_repo):
        """Two workers with separate worktrees cannot see each other's files."""
        async def _run():
            r1 = await WorktreeManager.create(git_repo, agent_type="worker-a")
            r2 = await WorktreeManager.create(git_repo, agent_type="worker-b")
            assert r1.worktree_path is not None
            assert r2.worktree_path is not None

            try:
                (r1.worktree_path / "output.txt").write_text("result-from-A")
                (r2.worktree_path / "output.txt").write_text("result-from-B")

                assert (r1.worktree_path / "output.txt").read_text() == "result-from-A"
                assert (r2.worktree_path / "output.txt").read_text() == "result-from-B"

                (r1.worktree_path / "temp_a.tmp").write_text("a")
                (r2.worktree_path / "temp_b.tmp").write_text("b")
                assert not (r1.worktree_path / "temp_b.tmp").exists()
                assert not (r2.worktree_path / "temp_a.tmp").exists()

                assert not (git_repo / "output.txt").exists()
                assert not (git_repo / "temp_a.tmp").exists()
                assert not (git_repo / "temp_b.tmp").exists()
            finally:
                await WorktreeManager.remove(r1.worktree_path)
                await WorktreeManager.remove(r2.worktree_path)

        asyncio.run(_run())

    def test_worktree_refs_are_distinct(self, git_repo):
        """Each worktree has a distinct git directory (isolation via separate checkout)."""
        async def _run():
            r1 = await WorktreeManager.create(git_repo, agent_type="agent-x")
            r2 = await WorktreeManager.create(git_repo, agent_type="agent-y")
            assert r1.worktree_path is not None
            assert r2.worktree_path is not None

            try:
                import subprocess

                def get_git_dir(path):
                    proc = subprocess.run(
                        ["git", "rev-parse", "--git-dir"],
                        capture_output=True, text=True, timeout=10, cwd=path,
                    )
                    return proc.stdout.strip()

                # Each worktree has its own git metadata directory.
                d1 = get_git_dir(r1.worktree_path)
                d2 = get_git_dir(r2.worktree_path)
                assert d1 != d2
            finally:
                await WorktreeManager.remove(r1.worktree_path)
                await WorktreeManager.remove(r2.worktree_path)

        asyncio.run(_run())
