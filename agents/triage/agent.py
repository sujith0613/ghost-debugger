import logging
import re
from agents.state.postmortem_state import PostmortemState
from agents.shared.llm import get_llm_with_tools
from agents.shared.node_utils import (
    run_react_loop, build_base_messages, safe_append, now_iso,
)
from agents.tools.registry import TRIAGE_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the TRIAGE AGENT in an automated incident analysis system.

Your ONLY job is to assess the SCOPE and SEVERITY of a reported incident.

YOUR RESPONSIBILITIES:
1. For each suspected service, query its current error rate and latency
2. Confirm which services are actually experiencing elevated error rates
3. Determine incident severity based on the data:
   - SEV1: error_rate > 20% OR multiple services simultaneously affected
   - SEV2: error_rate > 5% on one or two services
   - SEV3: error_rate 2-5% OR latency spike without error rate increase
4. Estimate the time window when the incident began

INVESTIGATION APPROACH:
- Start with the services mentioned in the alert
- Query error rate AND latency for each service
- If a service shows normal error rate (<2%), it is NOT affected — exclude it
- Check p99 latency for services with elevated errors

OUTPUT FORMAT:
After your investigation, provide a structured summary:

## Triage Findings
- [finding 1 with specific numbers]
- [finding 2 with specific numbers]

## Severity Assessment
SEVERITY: [SEV1/SEV2/SEV3]
REASON: [specific reason with numbers]

## Confirmed Affected Services
- [service_name]: [error_rate]% error rate
- (exclude services with <2% error rate)

## Time Window
INCIDENT_START: [ISO 8601 timestamp or "unknown"]
ANALYSIS_WINDOW: [start] to [end]

Be concise. Use numbers. Do not speculate beyond what the data shows."""


def triage_agent_node(state: PostmortemState) -> dict:
    logger.info(f"[triage] starting — incident: {state['incident_id']}")

    suspected_services = state.get("affected_services", [])
    trigger_description = state.get("trigger_description", "")
    detected_at = state.get("detected_at", "")
    analysis_window = state.get("analysis_window_seconds", 600)
    lookback = max(analysis_window // 60, 15)

    human_prompt = f"""INCIDENT REPORT:

Incident ID: {state['incident_id']}
Trigger Type: {state['trigger_type']}
Trigger Description: {trigger_description}
Detected At: {detected_at}
Suspected Services: {', '.join(suspected_services)}
Analysis Window: {lookback} minutes

Investigate this incident. Query error rates and request rates for each
suspected service. Confirm which services are actually affected.
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
        time_window = _extract_time_window(final_text, detected_at, analysis_window)
        findings = _extract_findings(final_text)

        logger.info(f"[triage] complete — severity={severity}, services={confirmed_services}")

        return {
            "triage_findings": findings,
            "triage_severity": severity,
            "triage_time_window": time_window,
            "triage_confirmed_services": confirmed_services,
            "completed_agents": safe_append(state.get("completed_agents", []), "triage"),
            "analysis_start_at": now_iso(),
        }

    except Exception as e:
        logger.error(f"[triage] failed: {e}")
        return {
            "triage_findings": [f"Triage investigation failed: {str(e)[:200]}"],
            "triage_severity": "UNKNOWN",
            "triage_time_window": "",
            "triage_confirmed_services": list(suspected_services),
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
    from datetime import datetime, timezone, timedelta
    try:
        detected = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        start = detected - timedelta(seconds=window_seconds)
        return f"{start.isoformat()} to {detected.isoformat()}"
    except Exception:
        return f"last {window_seconds // 60} minutes before {detected_at}"


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
