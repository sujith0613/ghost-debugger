import logging
import re
from datetime import datetime, timezone
from agents.state.postmortem_state import PostmortemState
from agents.shared.llm import get_llm_with_tools
from agents.shared.node_utils import (
    run_react_loop, build_base_messages, safe_append, now_iso,
    is_empty_tool_result, summarize_empty_signals,
)
from agents.tools.registry import TRIAGE_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the TRIAGE AGENT in an automated incident analysis system.

Your ONLY job is to assess the SCOPE and SEVERITY of a reported incident.

CRITICAL RULE - DATA INTEGRITY:
If a tool returns empty data (total_traces=0, latest_value=0, data_point_count=0),
that means NO DATA IS AVAILABLE from that backend. You MUST NOT invent or
estimate values. Write exactly what the tool returned.

If ALL tools return empty data:
  - Set severity to UNKNOWN
  - Write finding: "No telemetry data available - services may not be running
    or not sending telemetry to Ghost Debugger"
  - List NO confirmed services

SEVERITY RULES (only apply when you have real non-zero data):
  - SEV1: error_rate > 20% OR multiple services simultaneously affected
  - SEV2: error_rate > 5% on one or two services
  - SEV3: error_rate 2-5% OR latency-only anomaly

OUTPUT FORMAT:
## Triage Findings
- [finding with actual numbers from tools, or "No data available"]

## Severity Assessment
SEVERITY: [SEV1/SEV2/SEV3/UNKNOWN]
REASON: [specific reason, or "Insufficient telemetry data"]

## Confirmed Affected Services
- [service_name]: [actual error_rate]% error rate
(empty if no data available)

## Time Window
INCIDENT_START: [timestamp or "unknown - no telemetry data"]"""


def triage_agent_node(state: PostmortemState) -> dict:
    logger.info(f"[triage] starting - incident: {state['incident_id']}")
    start_time = now_iso()

    suspected_services = state.get("affected_services", [])
    lookback = max(state.get("analysis_window_seconds", 600) // 60, 15)

    # Pre-flight: check if any data exists before calling LLM
    from agents.tools.registry import get_querier
    from agents.storage.base import QueryError

    data_available = False
    preflight_finding = ""

    if suspected_services:
        try:
            querier = get_querier()
            ts = querier.query_error_rate(suspected_services[0], lookback_minutes=lookback)
            if ts.get("data_point_count", 0) > 0 or ts.get("latest_value", 0.0) > 0:
                data_available = True
            else:
                traces = querier.query_traces(
                    suspected_services[0], lookback_minutes=lookback, limit=5
                )
                if traces:
                    data_available = True
        except (QueryError, Exception) as e:
            preflight_finding = f"Backend query failed during preflight: {str(e)[:100]}"
            logger.warning(f"[triage] preflight check failed: {e}")

    if not data_available and not preflight_finding:
        preflight_finding = (
            f"No telemetry data found for {', '.join(suspected_services)} "
            f"in the last {lookback} minutes. "
            "Services may not be running or not sending telemetry."
        )

    if not data_available:
        logger.warning(f"[triage] no telemetry data - returning early")
        return {
            "triage_findings": [preflight_finding],
            "triage_severity": "UNKNOWN",
            "triage_time_window": "",
            "triage_confirmed_services": [],
            "completed_agents": ["triage"],
            "errors": [f"[triage] No telemetry data: {preflight_finding}"],
            "analysis_start_at": start_time,
        }

    # Normal path: data exists, run full ReAct loop
    human_prompt = f"""INCIDENT REPORT:

Incident ID: {state['incident_id']}
Trigger Type: {state['trigger_type']}
Trigger Description: {state.get('trigger_description', '')}
Detected At: {state.get('detected_at', '')}
Suspected Services: {', '.join(suspected_services)}
Analysis Window: {lookback} minutes

Investigate this incident. Query error rates and latency for each suspected service.
Only report what the tools actually return. Do not invent values.
Provide your structured triage assessment."""

    llm = get_llm_with_tools(TRIAGE_TOOLS)
    messages = build_base_messages(SYSTEM_PROMPT, human_prompt)

    try:
        final_text, tool_calls = run_react_loop(
            llm_with_tools=llm,
            tools=TRIAGE_TOOLS,
            messages=messages,
            agent_name="triage",
            max_iterations=8,
        )

        severity = _extract_severity(final_text)
        confirmed_services = _extract_confirmed_services(final_text, suspected_services)
        time_window = _extract_time_window(final_text, state.get("detected_at", ""),
                                           state.get("analysis_window_seconds", 600))
        findings = _extract_findings(final_text)

        logger.info(f"[triage] complete - severity={severity}, services={confirmed_services}")

        return {
            "triage_findings": findings,
            "triage_severity": severity,
            "triage_time_window": time_window,
            "triage_confirmed_services": confirmed_services,
            "completed_agents": safe_append(state.get("completed_agents", []), "triage"),
            "analysis_start_at": start_time,
        }

    except Exception as e:
        logger.error(f"[triage] failed: {e}")
        return {
            "triage_findings": [f"Triage investigation failed: {str(e)[:200]}"],
            "triage_severity": "UNKNOWN",
            "triage_time_window": "",
            "triage_confirmed_services": [],
            "completed_agents": safe_append(state.get("completed_agents", []), "triage"),
            "failed_agents": safe_append(state.get("failed_agents", []), "triage"),
            "errors": safe_append(state.get("errors", []), f"[triage] {type(e).__name__}: {str(e)[:200]}"),
        }


def _extract_severity(text: str) -> str:
    for pattern in [r"SEVERITY:\s*(SEV[123])", r"\b(SEV[123])\b"]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return "UNKNOWN"


def _extract_confirmed_services(text: str, suspected: list) -> list:
    confirmed = []
    section_match = re.search(
        r"## Confirmed Affected Services\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    if section_match:
        section = section_match.group(1)
        for service in ["service_a", "service_b", "service_c"]:
            if service in section:
                confirmed.append(service)
    if not confirmed:
        for service in suspected:
            if service in text:
                confirmed.append(service)
    return confirmed if confirmed else list(suspected)


def _extract_time_window(text: str, detected_at: str, window_seconds: int) -> str:
    match = re.search(r"ANALYSIS_WINDOW:\s*(.+?)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    from datetime import timedelta
    try:
        detected = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        start = detected - timedelta(seconds=window_seconds)
        return f"{start.isoformat()} to {detected.isoformat()}"
    except Exception:
        return ""


def _extract_findings(text: str) -> list:
    findings = []
    section_match = re.search(
        r"## Triage Findings\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    if section_match:
        for line in section_match.group(1).strip().split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line:
                findings.append(line)
    return findings if findings else [text[:500]]
