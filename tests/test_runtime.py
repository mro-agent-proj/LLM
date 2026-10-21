import asyncio

import pytest

from app.agent.demo import DemoTools, ScriptedDemoModel
from app.agent.runtime import AgentRuntime, get_runtime, using_runtime


@pytest.mark.asyncio
async def test_public_node_runtime_binding_is_isolated_between_async_requests():
    first = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools())
    second = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools(empty=True))

    async def check(runtime):
        with using_runtime(runtime):
            await asyncio.sleep(0)
            assert get_runtime() is runtime
            with using_runtime(second if runtime is first else first):
                assert get_runtime() is not runtime
            assert get_runtime() is runtime

    await asyncio.gather(check(first), check(second))
