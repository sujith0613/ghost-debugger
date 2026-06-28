import logging
import re
from agents.state.postmortem_state import PostmortemState
from agents.shared.llm import get_llm_with_tools
from agents.shared.node_utils import (
    run_react_loop, build_base_messages, safe_append,
)
from agents.tools.registry import METRIC_REASONER_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the METRIC ANALYSIS AGENT in an automated incident analysis system.

You specialize in time-series metrics analysis. Your job is to find resource
saturation events and quantify the impact of the incident.

YOUR RESPONSIBILITIES:
1. For each confirmed affected service, systematically query:
   - Error rate
   - p99 latency and p50 latency
   - Request rate
   - DB connections (if available)
   - Memory usage
   - Goroutine count (if available)
2. Identify which metric first became anomalous
3. Identify the specific SATURATED RESOURCE if any:
   - db_connections: connections == pool max (typically 100)
   - memory: approaching container limit (512MB)
   - goroutines: >10,000

INTERPRETATION GUIDE:
- DB connections at max + high p99 + error rate spike -> DB pool exhaustion
- Memory growing steadily -> memory leak
- Goroutines growing + p99 spike -> goroutine leak from slow downstream
- Request rate spike + error rate spike -> traffic overload
- Error rate spike + normal request rate -> internal failure (not overload)

OUTPUT FORMAT:

## Metric Analysis Findings
- [finding with specific metric values and timestamps]

## Resource Saturation
SATURATED_RESOURCE: [db_connections/memory/goroutines/cpu/none]
SATURATION_DETAIL: [specific values and timestamps]

## Anomaly Timeline
FIRST_ANOMALY: [metric_name] became anomalous at [time] - value: [X]
SECOND_ANOMALY: [metric_name] at [time] - value: [X]

## Traffic Pattern
TRAFFIC: [normal/spike/drop] - request rate [current] vs baseline [baseline]"""


def metric_reasoner_node(state: PostmortemState) -> dict:
    logger.info(f"[metric_reasoner] starting — incident: {state['incident_id']}")

    confirmed_services = state.get("triage_confirmed_services") or state.get("affected_services", [])
    time_window = state.get("triage_time_window", "")

    human_prompt = f"""METRIC ANALYSIS TASK:

Incident ID: {state['incident_id']}
Trigger Type: {state.get('trigger_type', '')}
Trigger Description: {state.get('trigger_description', '')}
Confirmed Affected Services: {', '.join(confirmed_services)}
Time Window: {time_window}

Systematically query metrics for each affected service.
For each service, check: error_rate, p99 latency, p50 latency, request_rate,
db_connections, memory, goroutines.

Find which resource is saturated or which metric first became anomalous.
Provide structured metric analysis with specific values."""

    llm = get_llm_with_tools(METRIC_REASONER_TOOLS)
    messages = build_base_messages(SYSTEM_PROMPT, human_prompt)

    try:
        final_text, tool_calls = run_react_loop(
            llm_with_tools=llm,
            tools=METRIC_REASONER_TOOLS,
            messages=messages,
            agent_name="metric_reasoner",
            max_iterations=10,
        )

        findings = _extract_findings(final_text)
        saturated_resource = _extract_saturated_resource(final_text)
        anomaly_details = _extract_anomaly_details(final_text)

        logger.info(f"[metric_reasoner] complete — saturated: {saturated_resource}")

        return {
            "metric_findings": findings,
            "metric_saturated_resource": saturated_resource,
            "metric_anomaly_details": anomaly_details,
            "metric_had_error": True,
            "completed_agents": safe_append(state.get("completed_agents", []), "metric_reasoner"),
        }

    except Exception as e:
        logger.error(f"[metric_reasoner] failed: {e}")
        return {
            "metric_findings": [f"Metric analysis unavailable: {str(e)[:200]}"],
            "metric_saturated_resource": "",
            "metric_anomaly_details": [],
            "metric_had_error": False,
            "completed_agents": safe_append(state.get("completed_agents", []), "metric_reasoner"),
            "failed_agents": safe_append(state.get("failed_agents", []), "metric_reasoner"),
            "errors": safe_append(state.get("errors", []), f"[metric_reasoner] {type(e).__name__}: {str(e)[:200]}"),
        }


def _extract_findings(text: str) -> list:
    findings = []
    section = re.search(r"## Metric Analysis Findings\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if section:
        for line in section.group(1).strip().split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line:
                findings.append(line)
    return findings or [text[:500]]


def _extract_saturated_resource(text: str) -> str:
    match = re.search(r"SATURATED_RESOURCE:\s*(\S+)", text, re.IGNORECASE)
    if match:
        resource = match.group(1).strip().lower()
        valid = ["db_connections", "memory", "goroutines", "cpu", "none"]
        return resource if resource in valid else "none"
    text_lower = text.lower()
    if "connection pool" in text_lower or "db connection" in text_lower:
        return "db_connections"
    if "memory" in text_lower and "oom" in text_lower:
        return "memory"
    if "goroutine" in text_lower:
        return "goroutines"
    return ""


def _extract_anomaly_details(text: str) -> list:
    details = []
    for pattern in [r"FIRST_ANOMALY:\s*(.+?)$", r"SECOND_ANOMALY:\s*(.+?)$"]:
        for match in re.finditer(pattern, text, re.MULTILINE):
            details.append(match.group(1).strip())
    if not details:
        section = re.search(r"## Anomaly Timeline\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
        if section:
            for line in section.group(1).strip().split("\n"):
                line = line.strip().lstrip("- ").strip()
                if line:
                    details.append(line)
    return details
