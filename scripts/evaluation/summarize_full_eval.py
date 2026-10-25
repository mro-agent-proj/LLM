"""Recount a saved live evaluation offline; never call a model, MCP, Backend or DB."""

import argparse
import collections
import hashlib
import json
import statistics
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ratio(n, d):
    return {"count": n, "total": d, "percent": round(100 * n / d, 2) if d else None}


def score(row):
    if row.get("error") or row.get("timed_out") or row.get("evaluation_error"):
        return False
    if row["expected_status"] == "HUMAN_REVIEW":
        return row["status"] == "HUMAN_REVIEW"
    return (
        row["status"] == "ASSIGNED"
        and row["department"] in row["acceptable"]
        and not row["trap_cited"]
    )


def metrics(rows):
    assigned = [r for r in rows if r.get("status") == "ASSIGNED"]
    expected_assigned = [r for r in rows if r["expected_status"] == "ASSIGNED"]
    expected_human = [r for r in rows if r["expected_status"] == "HUMAN_REVIEW"]
    human = [r for r in rows if r.get("status") == "HUMAN_REVIEW"]
    seconds = sorted(r["seconds"] for r in rows if "seconds" in r)
    return {
        "label_agreement": ratio(sum(r["correct"] for r in rows), len(rows)),
        "automatic_assignment_precision": ratio(sum(r["correct"] for r in assigned), len(assigned)),
        "incorrect_automatic_assignments": sum(not r["correct"] for r in assigned),
        "assignable_case_success": ratio(
            sum(r["correct"] for r in expected_assigned), len(expected_assigned)
        ),
        "human_review_recall": ratio(
            sum(r["correct"] for r in expected_human), len(expected_human)
        ),
        "human_expected_but_assigned": sum(r["status"] == "ASSIGNED" for r in expected_human),
        "human_review_count": len(human),
        "human_review_reasons": dict(collections.Counter(r.get("review_reason") for r in human)),
        "path_agreement": ratio(sum(r.get("path") == r["expected_path"] for r in rows), len(rows)),
        "trap_history_citations": sum(bool(r.get("trap_cited")) for r in rows),
        "client_timeout_or_submission_failure": sum(
            bool(r.get("timed_out")) or "error" in r for r in rows
        ),
        "system_error_results": sum(r.get("review_reason") == "SYSTEM_ERROR" for r in rows),
        "median_seconds": statistics.median(seconds) if seconds else None,
        "p95_seconds": seconds[(95 * len(seconds) + 99) // 100 - 1] if seconds else None,
        "sum_request_seconds": round(sum(seconds), 1),
    }


def summarize(bundle, dataset=None):
    bundle = Path(bundle)
    saved = read(bundle / "full-eval-results.json")
    audit = read(bundle / "full-checkpoint-audit.json")
    rows = saved["scenarios"]
    by_id = {r["id"]: r for r in rows}
    require(len(rows) == len(by_id) == saved["freeze"]["total"], "Missing/duplicate cases")
    require(len({r["request_id"] for r in rows}) == len(rows), "Duplicate request IDs")
    for row in rows:
        require(bool(score(row)) == row["correct"], f"Score mismatch: {row['id']}")
    if dataset:
        raw = Path(dataset).read_bytes()
        require(
            hashlib.sha256(raw).hexdigest() == saved["freeze"]["source_sha256"],
            "Dataset hash differs from the evaluated dataset",
        )
        cases = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        require(
            len(cases) == len(by_id) and {c["id"] for c in cases} == set(by_id),
            "Dataset case IDs differ",
        )
        for case in cases:
            row, expected = by_id[case["id"]], case["expected"]
            acceptable = expected.get("acceptable_departments") or (
                [expected["department"]] if expected.get("department") else []
            )
            require(
                row["expected_status"] == expected["status"]
                and row["expected_department"] == expected["department"]
                and row["acceptable"] == acceptable,
                f"Label mismatch: {case['id']}",
            )
            require(
                row["trap_cited"]
                == sorted(set(row["cited"]) & set(expected.get("must_not_cite", []))),
                f"Trap mismatch: {case['id']}",
            )
        labels = dict(collections.Counter(c["label_check"]["human"] for c in cases))
        require(labels == saved["summary"]["label_review_status"], "Label status mismatch")

    requests = audit["requests"]
    require(
        len(requests) == len(rows)
        and {r["request_id"] for r in requests} == {r["request_id"] for r in rows},
        "Audit request IDs do not match the evaluation",
    )
    tools = collections.defaultdict(collections.Counter)
    boundaries = []
    for item in requests:
        require(item["completed"], f"Incomplete checkpoint: {item['request_id']}")
        require(
            item["llm2_history_records"] == item["llm2_b02_calls"] == 0,
            f"LLM2 history/tool violation: {item['request_id']}",
        )
        for boundary in item["llm2_boundaries"]:
            require(
                not any(
                    boundary[k]
                    for k in (
                        "history_records",
                        "b02_traces",
                        "classification_comparisons",
                        "review_comparisons",
                        "forbidden_channels",
                    )
                ),
                "Investigator boundary violation",
            )
            boundaries.append(boundary)
        for node, counts in item["node_tool_counts"].items():
            tools[node].update(counts)
    tool_counts = {node: dict(counts) for node, counts in tools.items()}
    llm2 = saved["llm2_results"]
    require(len({r["case_id"] for r in llm2}) == len(llm2), "Duplicate LLM2 outputs")
    for item in llm2:
        row = by_id[item["case_id"]]
        matches = row["expected_status"] == "ASSIGNED" and item["department"] in row["acceptable"]
        require(matches == item["department_matches_label"], "LLM2 department score mismatch")
        require(
            item["expected_assignable"] == (row["expected_status"] == "ASSIGNED")
            and item["final_status"] == row["status"]
            and item["final_correct"] == row["correct"],
            "LLM2 final result mismatch",
        )
    eligible = [r for r in llm2 if r["expected_assignable"]]
    overall = metrics(rows)
    require(overall == saved["summary"]["overall"], "Frozen overall summary differs")
    require(tool_counts == saved["summary"]["tool_calls_by_node"], "Tool count mismatch")
    routes = {
        name: [r for r in rows if ("INVESTIGATING" in r["statuses"]) == investigate]
        for name, investigate in (("llm1_direct", False), ("llm23", True))
    }
    route_counts = {
        name: {
            "cases": len(items),
            "passed_assignments": sum(r["status"] == "ASSIGNED" and r["correct"] for r in items),
            "failed_assignments": sum(
                r["status"] == "ASSIGNED" and not r["correct"] for r in items
            ),
            "human_review": sum(r["status"] == "HUMAN_REVIEW" for r in items),
        }
        for name, items in routes.items()
    }
    failures = collections.Counter()
    for row in rows:
        if row["status"] != "ASSIGNED" or row["correct"]:
            continue
        category = (
            "expected_human_review"
            if row["expected_status"] == "HUMAN_REVIEW"
            else "wrong_department"
            if row["department"] not in row["acceptable"]
            else "forbidden_history"
            if row["trap_cited"]
            else "evaluation_error"
        )
        failures[category] += 1
    validations = saved["summary"]["validation_cases"]
    for row in validations:
        correct = row["http_status"] == 422 and any(
            all(item.get(k) == v for k, v in row["expected"].items()) for item in row["details"]
        )
        require(correct == row["correct"], f"Validation score mismatch: {row['id']}")
    return {
        "evaluation_date": "2026-10-23",
        "model": saved["freeze"]["model"],
        "cases": len(rows),
        "overall": overall,
        "routes": route_counts,
        "failed_assignment_categories": dict(failures),
        "by_split": {s: metrics([r for r in rows if r["split"] == s]) for s in ("dev", "test")},
        "by_group": {g: metrics([r for r in rows if r["group"] == g]) for g in "ABCD"},
        "llm2_output_cases": len(llm2),
        "llm2_department_agreement": ratio(
            sum(r["department_matches_label"] for r in eligible), len(eligible)
        ),
        "correct_llm2_department_held": sum(
            r["department_matches_label"] and r["final_status"] == "HUMAN_REVIEW" for r in llm2
        ),
        "checkpoint_audit": {
            "requests": len(requests),
            "input_boundaries": len(boundaries),
            "history_records": 0,
            "b02_calls": 0,
        },
        "tool_calls_by_node": tool_counts,
        "validation": ratio(sum(r["correct"] for r in validations), len(validations)),
        "label_review_status": saved["summary"]["label_review_status"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, help="Optional original Backend scenario JSONL")
    parser.add_argument("--out", type=Path, help="Write recomputed JSON to a new file")
    parser.add_argument("--check", action="store_true", help="Compare with saved summary.json")
    args = parser.parse_args()
    result = summarize(args.bundle, args.dataset)
    if args.check:
        require(result == read(args.bundle / "summary.json"), "Published summary differs")
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        with args.out.open("x", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
