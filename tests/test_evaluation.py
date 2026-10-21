import pytest

from app.agent.demo import ScriptedDemoModel
from app.agent.evaluation import cases, evaluate


@pytest.mark.asyncio
async def test_scripted_evaluation_all_expected_routes_and_no_live_quality_claim():
    results = [await evaluate(case, ScriptedDemoModel(), live=False) for case in cases()]
    assert len(results) == 8
    assert all(r["expected_workflow_matched"] for r in results)
    assert all(r["evaluation_kind"] == "scripted_contract_regression" for r in results)
    assert all(r["token_usage"] is None for r in results)
    assert all(r["data_origin"] == "synthetic" for r in results)
