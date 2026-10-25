"""Offline artifact checks; these synthetic fixtures do not measure model accuracy."""

import json
import sqlite3

import pytest

from scripts.evaluation.audit_live_isolation import audit_snapshot, snapshot_sqlite
from scripts.evaluation.summarize_full_eval import metrics, score, summarize


@pytest.fixture
def bundle(tmp_path):
    row = {
        "id": "case-1",
        "request_id": "request-1",
        "status": "ASSIGNED",
        "expected_status": "ASSIGNED",
        "department": "HYD",
        "expected_department": "HYD",
        "acceptable": ["HYD"],
        "trap_cited": [],
        "cited": [],
        "correct": True,
        "path": "LLM1",
        "expected_path": "LLM1",
        "statuses": ["CLASSIFYING", "ASSIGNED"],
        "seconds": 1.0,
        "split": "dev",
        "group": "A",
    }
    results = {
        "freeze": {"total": 1, "model": "synthetic"},
        "scenarios": [row],
        "llm2_results": [],
        "summary": {
            "overall": metrics([row]),
            "tool_calls_by_node": {},
            "validation_cases": [],
            "label_review_status": {},
        },
    }
    audit = {
        "requests": [
            {
                "request_id": "request-1",
                "completed": True,
                "llm2_history_records": 0,
                "llm2_b02_calls": 0,
                "llm2_boundaries": [],
                "node_tool_counts": {},
            }
        ]
    }
    (tmp_path / "full-eval-results.json").write_text(json.dumps(results))
    (tmp_path / "full-checkpoint-audit.json").write_text(json.dumps(audit))
    return tmp_path, results, audit


def test_recount_and_trap_are_separate_from_department(bundle):
    path, results, _ = bundle
    assert summarize(path)["overall"]["label_agreement"]["count"] == 1
    row = {**results["scenarios"][0], "trap_cited": ["MH-trap"]}
    assert not score(row)
    assert not score({**row, "expected_status": "HUMAN_REVIEW"})
    assert score({**row, "expected_status": "HUMAN_REVIEW", "status": "HUMAN_REVIEW"})
    assert not score({**row, "evaluation_error": "TRACE_UNAVAILABLE"})


@pytest.mark.parametrize("mutation", ["duplicate", "score", "summary", "dataset"])
def test_recount_rejects_inconsistent_results(bundle, mutation):
    path, results, _ = bundle
    dataset = None
    if mutation == "duplicate":
        results["scenarios"].append(results["scenarios"][0])
    elif mutation == "score":
        results["scenarios"][0]["trap_cited"] = ["MH-trap"]
    elif mutation == "summary":
        results["summary"]["overall"]["label_agreement"]["count"] = 0
    else:
        results["freeze"]["source_sha256"] = "not-the-dataset-hash"
        dataset = path / "dataset.jsonl"
        dataset.write_text("{}\n")
    (path / "full-eval-results.json").write_text(json.dumps(results))
    with pytest.raises(ValueError):
        summarize(path, dataset)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "foreign-request"),
        ("completed", False),
        ("llm2_history_records", 1),
        ("llm2_b02_calls", 1),
        ("node_tool_counts", {"investigate": {"search_sdr_cases": 1}}),
    ],
)
def test_recount_rejects_inconsistent_audit(bundle, field, value):
    path, _, audit = bundle
    audit["requests"][0][field] = value
    (path / "full-checkpoint-audit.json").write_text(json.dumps(audit))
    with pytest.raises(ValueError):
        summarize(path)


def test_checkpoint_backup_includes_committed_wal_and_preserves_source(tmp_path):
    source, target = tmp_path / "source.sqlite", tmp_path / "copy.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('committed')")
        connection.commit()
        snapshot_sqlite(source, target)
        with sqlite3.connect(target) as copied:
            assert copied.execute("SELECT value FROM sample").fetchone() == ("committed",)
            copied.execute("DELETE FROM sample")
        assert connection.execute("SELECT value FROM sample").fetchone() == ("committed",)


def test_checkpoint_backup_never_creates_missing_source(tmp_path):
    source = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        snapshot_sqlite(source, tmp_path / "copy.sqlite")
    assert not source.exists()


@pytest.mark.asyncio
async def test_audit_reads_saved_graph_and_requires_selected_request(tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from app.agent.runtime import AgentRuntime
    from app.agent.workflow import build_graph
    from app.service import MaintenanceService
    from tests.support import DemoTools, ScriptedDemoModel
    from tests.test_service import request

    run = request()
    source, snapshot = tmp_path / "live.sqlite", tmp_path / "snapshot.sqlite"

    async def publish(_):
        pass

    async with AsyncSqliteSaver.from_conn_string(str(source)) as saver:
        await MaintenanceService(saver, graph_factory=build_graph).execute(
            run, AgentRuntime(ScriptedDemoModel(), DemoTools()), publish
        )
    snapshot_sqlite(source, snapshot)
    audit = await audit_snapshot(snapshot, {str(run.request_id)})
    assert len(audit) == 1 and audit[0]["completed"]
    assert audit[0]["llm2_boundaries"]
    assert audit[0]["llm2_history_records"] == audit[0]["llm2_b02_calls"] == 0
    assert audit[0]["node_tool_counts"]["investigate"]["search_sdr_cases"] == 1
    assert "SYNTHETIC" not in json.dumps(audit)  # source text is not exported
    with pytest.raises(ValueError, match="Missing or ambiguous"):
        await audit_snapshot(snapshot, {"missing-request"})
