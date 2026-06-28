#!/usr/bin/env python3
"""Analyze scenario results and generate a summary report.

Usage: python3 scripts/analyze_results.py [docs/postmortem-examples/]
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime


def analyze_state_file(path: Path) -> dict:
    with open(path) as f:
        state = json.load(f)

    completed = list(set(state.get("completed_agents", [])))
    failed = list(set(state.get("failed_agents", [])))
    errors = state.get("errors", [])

    signals = {
        "trace": state.get("trace_had_error", False),
        "log": state.get("log_had_error", False),
        "metric": state.get("metric_had_error", False),
    }
    available_signals = sum(1 for v in signals.values() if v)

    return {
        "incident_id": state.get("incident_id", "unknown"),
        "trigger": state.get("trigger_description", "")[:100],
        "status": state.get("status", "unknown"),
        "severity": state.get("triage_severity", "UNKNOWN"),
        "root_cause": state.get("root_cause", "")[:150],
        "confidence": state.get("root_cause_confidence", 0.0),
        "signal_completeness": state.get("signal_completeness", "unknown"),
        "available_signals": available_signals,
        "signals": signals,
        "completed_agents": completed,
        "failed_agents": failed,
        "error_count": len(errors),
        "errors": errors[:3],
        "report_length": len(state.get("postmortem_report", "")),
        "confirmed_services": state.get("triage_confirmed_services", []),
        "first_error_service": state.get("trace_first_error_service", ""),
        "saturated_resource": state.get("metric_saturated_resource", ""),
        "similar_incidents": state.get("similar_incidents", []),
        "causal_chain": state.get("causal_chain", []),
        "timeline": state.get("timeline", []),
    }


def grade_analysis(result: dict, expected: dict) -> dict:
    grades = {}
    grades["pipeline_complete"] = result["status"] == "complete"
    if expected.get("severity"):
        grades["severity_correct"] = result["severity"] == expected["severity"]
    if expected.get("root_cause_keywords"):
        rc_lower = result["root_cause"].lower()
        grades["root_cause_identified"] = any(
            kw.lower() in rc_lower for kw in expected["root_cause_keywords"]
        )
    grades["confidence_adequate"] = result["confidence"] >= 0.5
    grades["signals_available"] = result["available_signals"] >= 2
    grades["report_generated"] = result["report_length"] > 500
    grades["no_critical_failure"] = "postmortem_writer" in result["completed_agents"]
    return grades


def print_scenario_report(scenario_name: str, result: dict, grades: dict):
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  {scenario_name}")
    print(f"{sep}")

    all_pass = all(grades.values())
    critical = grades.get("pipeline_complete", False) and grades.get("report_generated", False)

    if all_pass:
        verdict = "[PASS] All checks passed"
    elif critical:
        verdict = "[PARTIAL] Pipeline completed with some gaps"
    else:
        verdict = "[FAIL] Pipeline did not complete correctly"

    print(f"\n  {verdict}")
    print(f"\n  Incident: {result['incident_id']}")
    print(f"  Status:   {result['status']}")
    print(f"  Severity: {result['severity']}")
    print(f"  Confidence: {result['confidence']:.0%}")

    print(f"\n  Root Cause:")
    print(f"    {result['root_cause'] or '(not determined)'}")

    print(f"\n  Signals Available:")
    for sig, avail in result["signals"].items():
        icon = "+" if avail else "x"
        print(f"    {icon} {sig}")

    print(f"\n  Completed Agents: {result['completed_agents']}")
    if result["failed_agents"]:
        print(f"  Failed Agents:    {result['failed_agents']}")

    if result["similar_incidents"]:
        print(f"\n  Similar Past Incidents Found: {result['similar_incidents']}")

    if result["causal_chain"]:
        print(f"\n  Causal Chain ({len(result['causal_chain'])} steps):")
        for step in result["causal_chain"][:3]:
            print(f"    -> {step[:80]}")

    print(f"\n  Report: {result['report_length']:,} chars generated")

    print(f"\n  Grade Breakdown:")
    for check, passed in grades.items():
        icon = "PASS" if passed else "FAIL"
        print(f"    {icon} {check}")

    if result["errors"]:
        print(f"\n  Pipeline Errors:")
        for err in result["errors"]:
            print(f"    [!] {err[:100]}")


def main():
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/postmortem-examples")

    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}")
        print("Run: python3 scripts/run_scenario.py")
        sys.exit(1)

    state_files = sorted(results_dir.glob("*_state.json"))

    if not state_files:
        print(f"No result files found in {results_dir}")
        print("Run: python3 scripts/run_scenario.py")
        sys.exit(1)

    EXPECTED = {
        "cascade": {
            "severity": "SEV1",
            "root_cause_keywords": ["latency", "cascade", "timeout", "service_b", "slow"],
        },
        "resource": {
            "severity": "SEV1",
            "root_cause_keywords": ["error", "pool", "exhausted", "resource", "service_b"],
        },
        "traffic": {
            "severity": "SEV2",
            "root_cause_keywords": ["traffic", "spike", "rate", "overload", "request"],
        },
    }

    sep = "=" * 62
    print(f"\n{sep}")
    print("  Ghost Debugger - Scenario Analysis Results")
    print(f"{sep}")

    all_grades = {}
    all_results = []

    for state_file in state_files:
        filename = state_file.stem
        scenario_type = "cascade"
        if "resource" in filename:
            scenario_type = "resource"
        elif "traffic" in filename:
            scenario_type = "traffic"

        try:
            result = analyze_state_file(state_file)
            expected = EXPECTED.get(scenario_type, {})
            grades = grade_analysis(result, expected)
            scenario_label = f"Scenario: {scenario_type.upper()} ({filename})"
            print_scenario_report(scenario_label, result, grades)
            all_grades[filename] = grades
            all_results.append(result)
        except Exception as e:
            print(f"\n⚠ Failed to analyze {state_file.name}: {e}")

    if all_results:
        sep = "=" * 62
        print(f"\n{sep}")
        print("  OVERALL SUMMARY")
        print(f"{sep}")
        print(f"\n  Scenarios analyzed: {len(all_results)}")

        total_checks = sum(len(g) for g in all_grades.values())
        total_passed = sum(sum(g.values()) for g in all_grades.values())
        if total_checks:
            print(f"  Checks passed: {total_passed}/{total_checks} ({total_passed/total_checks*100:.0f}%)")

        complete_pipelines = sum(1 for r in all_results if r["status"] == "complete")
        print(f"  Complete pipelines: {complete_pipelines}/{len(all_results)}")

        avg_confidence = sum(r["confidence"] for r in all_results) / len(all_results) if all_results else 0
        print(f"  Average confidence: {avg_confidence:.0%}")

        full_signals = sum(1 for r in all_results if r["signal_completeness"] == "full")
        print(f"  Full signal coverage: {full_signals}/{len(all_results)}")

        print(f"\n  Result files: {results_dir}/")
        print(f"  Postmortems:  {results_dir}/*_postmortem.md")
    print()


if __name__ == "__main__":
    main()
