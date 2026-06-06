from __future__ import annotations

import pytest

from xxcode.benchmark.models import BenchmarkPlugin
from tests.benchmark.plugins.context import ContextBenchmarkPlugin
from tests.benchmark.plugins.memory import MemoryBenchmarkPlugin
from tests.benchmark.plugins.security import SecurityBenchmarkPlugin


@pytest.mark.asyncio
async def test_plugins_implement_protocol_and_return_typed_scorecards():
    memory = MemoryBenchmarkPlugin()
    context = ContextBenchmarkPlugin()
    security = SecurityBenchmarkPlugin()

    assert isinstance(memory, BenchmarkPlugin)
    assert isinstance(context, BenchmarkPlugin)
    assert isinstance(security, BenchmarkPlugin)

    memory_result = await memory.run_suite(None)
    context_result = await context.run_suite(None)
    security_result = await security.run_suite(None)

    assert memory_result.plugin == "memory"
    assert context_result.plugin == "context"
    assert security_result.plugin == "security"
    assert "memory_lift_rate" in memory_result.scorecard.metric_map()
    assert "budget_pass_rate" in context_result.scorecard.metric_map()
    assert "static_bypass_rate" in security_result.scorecard.metric_map()
