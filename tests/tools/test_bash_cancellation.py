"""Tests for Bash tool cancellation and process-group handling."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from xxcode.tools.BashTool import BashInput, BashTool
from xxcode.tools.BashTool import background as bg
from xxcode.tools.BashTool import sandbox as sb


class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None
        self.communicate_started = asyncio.Event()
        self.release_communicate = asyncio.Event()
        self.wait_calls = 0

    async def communicate(self):
        self.communicate_started.set()
        await self.release_communicate.wait()
        return b"", b""

    async def wait(self):
        self.wait_calls += 1
        return self.returncode or 0


def test_process_group_kwargs_are_platform_aware():
    kwargs = bg._process_group_kwargs()
    if bg._IS_WINDOWS:
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True


def test_bash_run_command_cancel_uses_terminate_process_tree_and_reaps(monkeypatch):
    tool = BashTool()
    proc = _FakeProcess(pid=1234)

    async def _fake_create_subprocess_shell(*args, **kwargs):
        return proc

    called: list[int] = []

    async def _fake_terminate(target):
        called.append(target.pid)
        target.returncode = -9

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create_subprocess_shell)
    monkeypatch.setattr(bg, "_terminate_process_tree", _fake_terminate)

    async def _run():
        task = asyncio.create_task(tool._run_command("sleep 30", ".", 30.0))
        await proc.communicate_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "not-cancelled"

    result = asyncio.run(_run())

    assert result == "cancelled"
    assert called == [1234]
    assert proc.wait_calls == 1


def test_bash_run_command_timeout_uses_terminate_process_tree_and_reaps(monkeypatch):
    tool = BashTool()
    proc = _FakeProcess(pid=2222)

    async def _fake_create_subprocess_shell(*args, **kwargs):
        return proc

    called: list[int] = []

    async def _fake_terminate(target):
        called.append(target.pid)
        target.returncode = -15
        target.release_communicate.set()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create_subprocess_shell)
    monkeypatch.setattr(bg, "_terminate_process_tree", _fake_terminate)

    result = asyncio.run(tool._run_command("sleep 30", ".", 0.01))

    assert result[0] == -1
    assert "timed out" in result[1]
    assert called == [2222]
    assert proc.wait_calls == 1


def test_sandboxed_execute_uses_subprocess_exec_and_reaps_on_cancel(monkeypatch, tmp_path):
    tool = BashTool()
    proc = _FakeProcess(pid=3333)
    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        return proc

    async def _fake_terminate(target):
        target.returncode = -9

    monkeypatch.setattr(sb, "should_use_sandbox", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        tool._sandbox_manager,
        "get_sandbox_command",
        lambda command, config, cwd: [
            "sandbox-exec",
            "-p",
            "(allow file-read*)",
            "sh",
            "-c",
            command,
        ],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(bg, "_terminate_process_tree", _fake_terminate)

    async def _run():
        task = asyncio.create_task(
            tool.execute(
                BashInput(command="printf hello"),
                {"cwd": str(tmp_path)},
            )
        )
        await proc.communicate_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "not-cancelled"

    result = asyncio.run(_run())

    assert result == "cancelled"
    assert captured["args"] == (
        "sandbox-exec",
        "-p",
        "(allow file-read*)",
        "sh",
        "-c",
        "printf hello",
    )
    assert captured["cwd"] == str(tmp_path)
    assert proc.wait_calls == 1


def test_background_cancel_uses_terminate_process_tree(monkeypatch, tmp_path):
    async def _run():
        manager = bg.BackgroundManager(tmp_path)
        proc = _FakeProcess(pid=5678)
        task_id = "bg-test-1"
        task = bg.BackgroundTask(
            task_id=task_id,
            command="echo hello",
            description="hello",
            output_path=str(tmp_path / "tool-results" / "bg-test-1.txt"),
            error_path=str(tmp_path / "tool-results" / "bg-test-1.error.txt"),
            status="running",
        )
        manager._tasks[task_id] = task
        manager._processes[task_id] = proc

        called: list[int] = []

        async def _fake_terminate(target):
            called.append(target.pid)
            target.returncode = -9

        monkeypatch.setattr(bg, "_terminate_process_tree", _fake_terminate)

        gate = asyncio.Event()

        async def _pending():
            await gate.wait()

        manager._asyncio_tasks[task_id] = asyncio.create_task(_pending())
        result = await manager.cancel(task_id)
        return result, called, proc.wait_calls

    result, called, wait_calls = asyncio.run(_run())

    assert result is True
    assert called == [5678]
    assert wait_calls == 1


def test_background_timeout_reaps_process_after_termination(monkeypatch, tmp_path):
    async def _run():
        manager = bg.BackgroundManager(tmp_path)
        proc = _FakeProcess(pid=6789)
        task = bg.BackgroundTask(
            task_id="bg-timeout",
            command="sleep 30",
            description="timeout",
            output_path=str(Path(tmp_path) / "tool-results" / "bg-timeout.txt"),
            error_path=str(Path(tmp_path) / "tool-results" / "bg-timeout.error.txt"),
            status="running",
        )

        async def _fake_create_subprocess_shell(*args, **kwargs):
            return proc

        called: list[int] = []

        async def _fake_terminate(target):
            called.append(target.pid)
            target.returncode = -15
            target.release_communicate.set()

        monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create_subprocess_shell)
        monkeypatch.setattr(bg, "_terminate_process_tree", _fake_terminate)

        await manager._run_task(task, str(tmp_path), timeout=0.01)
        return task, called, proc.wait_calls

    task, called, wait_calls = asyncio.run(_run())

    assert task.status == "failed"
    assert called == [6789]
    assert wait_calls == 1
