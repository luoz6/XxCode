from __future__ import annotations

import pytest

from tests.benchmark.plugins.context import ContextBenchmarkPlugin
from tests.benchmark.plugins.memory import MemoryBenchmarkPlugin
from tests.benchmark.plugins.security import SecurityBenchmarkPlugin


@pytest.mark.asyncio
async def test_memory_plugin_repeat_runs_are_stable():
    plugin = MemoryBenchmarkPlugin()

    first = await plugin.run_suite(None)
    second = await plugin.run_suite(None)

    assert first.scorecard.metric_map() == second.scorecard.metric_map()


@pytest.mark.asyncio
async def test_context_plugin_repeat_runs_are_stable():
    plugin = ContextBenchmarkPlugin()

    first = await plugin.run_suite(None)
    second = await plugin.run_suite(None)

    assert first.scorecard.metric_map() == second.scorecard.metric_map()


@pytest.mark.asyncio
async def test_security_plugin_repeat_runs_are_stable():
    plugin = SecurityBenchmarkPlugin()

    first = await plugin.run_suite(None)
    second = await plugin.run_suite(None)

    assert first.scorecard.metric_map() == second.scorecard.metric_map()
