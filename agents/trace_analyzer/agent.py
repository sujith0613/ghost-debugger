import logging
import re
from agents.state.postmortem_state import PostmortemState
from agents.shared.llm import get_llm_with_tools
from agents.shared.node_utils import (
    run_react_loop, build_base_messages, safe_append, format_state_for_prompt,
)
from agents.tools.registry import TRACE_ANALYZER_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the TRACE ANALYSIS AGENT in an automated incident analysis system.

You specialize in distributed tracing. Your job is to analyze request traces
to find WHERE failures originated and HOW they propagated.

YOUR RESPONSIBILITIES:
1. Query error traces for each confirmed affected service
2. Compare error rates and latency between services
3. Determine TEMPORAL ORDERING: which service showed errors FIRST?
4. Identify the cascade pattern: did service_b fail, then service_a?
5. Identify specific error types (DeadlineExceeded, ConnectionRefused, etc.)

CRITICAL INSIGHT:
In a call chain A -> B -> C:
- If B errors appear BEFORE A errors: B is the root cause location
- If A errors and B errors appear simultaneously: A may be the cause
- p99 latency spike in B with low p50 spike = resource contention in B

OUTPUT FORMAT:

## Trace Analysis Findings
- [finding with specific numbers and timestamps]

## Error Propagation
FIRST_ERROR_SERVICE: [service_name]
FIRST_ERROR_TIME: [ISO 8601 or approximate]
CASCADE_PATH: [service_b -> service_a] (origin first)

## Latency Analysis
- [p50 and p99 for each service with interpretation]

## Error Types
- [specific error message patterns observed]"""


def trace_analyzer_node(state: PostmortemState) -> dict:
    logger.info(f"[trace_analyzer] starting — incident: {state['incident_id']}")

    confirmed_services = state.get("triage_confirmed_services") or state.get("affected_services", [])
    time_window = state.get("triage_time_window", "")

    triage_context = format_state_for_prompt(state, [
        "triage_findings", "triage_severity", "triage_time_window",
    ])

    human_prompt = f"""TRACE ANALYSIS TASK:

Incident ID: {state['incident_id']}
Trigger: {state.get('trigger_description', '')}
Confirmed Affected Services: {', '.join(confirmed_services)}
Time Window: {time_window}

Triage Context:
{triage_context}

Query distributed traces for each affected service.
Determine which service showed errors FIRST.
Identify the cascade pattern and specific error types.
Provide structured trace analysis."""

    llm = get_llm_with_tools(TRACE_ANALYZER_TOOLS)
    messages = build_base_messages(SYSTEM_PROMPT, human_prompt)

    try:
        final_text, tool_calls = run_react_loop(
            llm_with_tools=llm,
            tools=TRACE_ANALYZER_TOOLS,
            messages=messages,
            agent_name="trace_analyzer",
            max_iterations=8,
        )

        findings = _extract_findings(final_text)
        first_error_service = _extract_first_error_service(final_text, confirmed_services)
        first_error_time = _extract_first_error_time(final_text)
        cascade_path = _extract_cascade_path(final_text, confirmed_services)

        logger.info(f"[trace_analyzer] complete — first_error_service={first_error_service}")

        return {
            "trace_findings": findings,
            "trace_first_error_service": first_error_service,
            "trace_first_error_time": first_error_time,
            "trace_cascade_path": cascade_path,
            "trace_had_error": True,
            "completed_agents": safe_append(state.get("completed_agents", []), "trace_analyzer"),
        }

    except Exception as e:
        logger.error(f"[trace_analyzer] failed: {e}")
        return {
            "trace_findings": [f"Trace analysis unavailable: {str(e)[:200]}"],
            "trace_first_error_service": "",
            "trace_first_error_time": "",
            "trace_cascade_path": [],
            "trace_had_error": False,
            "failed_agents": safe_append(state.get("failed_agents", []), "trace_analyzer"),
            "errors": safe_append(state.get("errors", []), f"[trace_analyzer] {type(e).__name__}: {str(e)[:200]}"),
        }


def _extract_findings(text: str) -> list:
    findings = []
    section = re.search(r"## Trace Analysis Findings\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if section:
        for line in section.group(1).strip().split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line:
                findings.append(line)
    return findings or [text[:500]]


def _extract_first_error_service(text: str, services: list) -> str:
    match = re.search(r"FIRST_ERROR_SERVICE:\s*(\S+)", text, re.IGNORECASE)
    if match:
        svc = match.group(1).strip()
        if any(s in svc for s in services):
            return svc
    for svc in services:
        if svc in text:
            return svc
    return services[0] if services else ""


def _extract_first_error_time(text: str) -> str:
    match = re.search(r"FIRST_ERROR_TIME:\s*(.+?)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    ts_match = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', text)
    return ts_match.group(0) if ts_match else ""


def _extract_cascade_path(text: str, services: list) -> list:
    match = re.search(r"CASCADE_PATH:\s*(.+?)$", text, re.MULTILINE)
    if match:
        path_str = match.group(1).strip()
        parts = re.split(r'\s*[>\-]+\s*', path_str)
        return [p.strip() for p in parts if any(s in p for s in services)]
    return list(services)
