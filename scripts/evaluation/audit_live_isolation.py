"""Export checkpoint metadata offline from a read-only SQLite backup.

Input boundaries are projections through InvestigatorState, not captured network inputs.
Use the graph isolation regression tests as a complementary check.
"""

import argparse
import asyncio
import collections
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def snapshot_sqlite(source, target):
    """SQLite backup also includes committed WAL data; never open source writable."""
    uri = Path(source).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as original:
        with sqlite3.connect(target) as copy:
            original.backup(copy)


async def audit_snapshot(snapshot, request_ids):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from app.agent.schemas import InvestigatorState

    reports = []
    async with AsyncSqliteSaver.from_conn_string(str(snapshot)) as saver:
        async with saver.conn.execute("SELECT DISTINCT thread_id FROM checkpoints") as cursor:
            threads = [row[0] for row in await cursor.fetchall()]
        selected = [t for t in threads if t.partition(":")[2] in request_ids]
        found = [t.partition(":")[2] for t in selected]
        if len(found) != len(set(found)) or set(found) != request_ids:
            raise ValueError("Missing or ambiguous checkpoint threads for selected requests")
        for thread in selected:
            checkpoints = [c async for c in saver.alist({"configurable": {"thread_id": thread}})]
            checkpoints.sort(key=lambda c: c.metadata.get("step", -99))
            boundaries = []
            node_tools = collections.defaultdict(collections.Counter)
            previous = {}
            for checkpoint in checkpoints:
                current = checkpoint.checkpoint["channel_values"]
                nodes = [
                    k.removeprefix("branch:to:") for k in previous if k.startswith("branch:to:")
                ]
                for node in nodes:
                    if node not in {"classify", "investigate", "review", "direct", "finalize"}:
                        continue
                    key = "llm2_context" if node == "investigate" else "agent_context"
                    before = previous.get(key, {}).get("traces", [])
                    after = current.get(key, {}).get("traces", [])
                    node_tools[node].update(t["tool"] for t in after[len(before) :])
                previous = current

            for checkpoint in checkpoints:
                state = checkpoint.checkpoint["channel_values"]
                if "branch:to:investigate" in state:
                    allowed = {k: state[k] for k in InvestigatorState.__annotations__ if k in state}
                    context = allowed.get("llm2_context", {})
                    boundary = {
                        "step": checkpoint.metadata["step"],
                        "input_keys": sorted(allowed),
                        "history_records": sum(
                            r["source"] == "HISTORY" for r in context.get("records", [])
                        ),
                        "b02_traces": sum(
                            t["tool"] == "get_aircraft_history" for t in context.get("traces", [])
                        ),
                        "classification_comparisons": len(
                            context.get("classification_comparisons", [])
                        ),
                        "review_comparisons": len(context.get("review_comparisons", [])),
                        "forbidden_channels": sorted(
                            set(allowed)
                            & {"agent_context", "classification", "reviews", "stage_events"}
                        ),
                    }
                    if any(
                        boundary[k]
                        for k in (
                            "history_records",
                            "b02_traces",
                            "classification_comparisons",
                            "review_comparisons",
                            "forbidden_channels",
                        )
                    ):
                        raise ValueError("LLM2 input boundary violation")
                    boundaries.append(boundary)
            final = checkpoints[-1].checkpoint["channel_values"]
            context = final.get("llm2_context", {})
            history = [r for r in context.get("records", []) if r["source"] == "HISTORY"]
            b02 = [t for t in context.get("traces", []) if t["tool"] == "get_aircraft_history"]
            if history or b02:
                raise ValueError("LLM2 history or B02 violation")
            joined = []
            for checkpoint in checkpoints:
                state = checkpoint.checkpoint["channel_values"]
                if "branch:to:review" in state:
                    records = state.get("agent_context", {}).get("records", [])
                    joined.append(
                        {
                            "history_records": sum(r["source"] == "HISTORY" for r in records),
                            "sdr_records": sum(r["source"] == "SDR" for r in records),
                            "classification_present": state.get("classification") is not None,
                            "investigations": len(state.get("investigations", [])),
                        }
                    )
            reports.append(
                {
                    "request_id": thread.split(":", 1)[1],
                    "workflow": thread.split(":", 1)[0],
                    "llm2_boundaries": boundaries,
                    "llm2_history_records": len(history),
                    "llm2_b02_calls": len(b02),
                    "llm2_tools": dict(
                        collections.Counter(t["tool"] for t in context.get("traces", []))
                    ),
                    "llm2_tool_results": [
                        {
                            "tool": t["tool"],
                            "outcome": t["outcome"],
                            "error_code": t.get("error_code"),
                        }
                        for t in context.get("traces", [])
                    ],
                    "review_joins": joined,
                    "node_tool_counts": {k: dict(v) for k, v in node_tools.items()},
                    "completed": any(
                        e["type"] in {"COMPLETED", "FAILED"} for e in final.get("stage_events", [])
                    ),
                }
            )
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-db", type=Path, required=True)
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="Saved full-eval-results.json selects the request IDs",
    )
    parser.add_argument("--provider", default="openai", help="Recorded metadata, not verification")
    parser.add_argument("--out", type=Path, required=True, help="New output file; never overwrite")
    args = parser.parse_args()
    saved = json.loads(args.results.read_text(encoding="utf-8"))
    rows = saved["scenarios"]
    ids = {r["request_id"] for r in rows}
    if not ids or len(ids) != len(rows):
        raise ValueError("Missing or duplicate request IDs")
    # AsyncSqliteSaver may create indexes/tables. Only its temporary backup is writable.
    with tempfile.TemporaryDirectory(prefix="mro-checkpoint-audit-") as directory:
        snapshot = Path(directory) / "checkpoints.sqlite"
        snapshot_sqlite(args.checkpoint_db, snapshot)
        requests = asyncio.run(audit_snapshot(snapshot, ids))
    order = {r["request_id"]: i for i, r in enumerate(rows)}
    requests.sort(key=lambda r: order[r["request_id"]])
    result = {"model": saved["freeze"]["model"], "provider": args.provider, "requests": requests}
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
