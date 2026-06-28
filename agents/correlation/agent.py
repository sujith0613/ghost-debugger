import logging
import re
from agents.state.postmortem_state import PostmortemState
from agents.shared.llm import get_llm_with_tools
from agents.shared.node_utils import (
    run_react_loop, build_base_messages, safe_append, format_state_for_prompt,
)
from agents.tools.registry import CORRELATION_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the CORRELATION AGENT in an automated incident analysis system.

You receive findings from three specialized agents (trace, log, metric)
and your job is to synthesize them into a coherent causal chain.

YOUR RESPONSIBILITIES:
1. Review all findings from trace analysis, log analysis, and metric analysis
2. Build a TEMPORAL TIMELINE: order all events by timestamp
3. Identify the ROOT CAUSE SIGNAL: the first anomaly that preceded all others
4. Construct the causal chain
5. Search similar past incidents
6. Note which signals are missing and adjust confidence

SYNTHESIS APPROACH:
- The earliest anomaly is the most likely root cause signal
- Metric anomalies often precede trace errors
- Log patterns confirm what the metrics and traces show
- If similar past incidents exist, their root causes are strong evidence

OUTPUT FORMAT:

## Correlation Summary
[2-3 paragraph narrative explaining how all signals relate and the causal chain]

## Causal Chain
STEP_1: [timestamp] - [what happened] ([signal source])
STEP_2: [timestamp] - [what happened] ([signal source])
STEP_3: [timestamp] - [what happened] ([signal source])

## Similar Past Incidents
[After searching, list matches with similarity score and root cause]

## Signal Completeness
SIGNALS_AVAILABLE: [trace=yes/no] [log=yes/no] [metric=yes/no]
CONFIDENCE_IMPACT: [if any signals missing, explain how this affects confidence]"""


def correlation_agent_node(state: PostmortemState) -> dict:
    logger.info(f"[correlation] starting — incident: {state['incident_id']}")

    trace_context = format_state_for_prompt(state, [
        "trace_findings", "trace_first_error_service",
        "trace_first_error_time", "trace_cascade_path",
    ])
    log_context = format_state_for_prompt(state, [
        "log_findings", "log_error_patterns", "log_first_error_time",
    ])
    metric_context = format_state_for_prompt(state, [
        "metric_findings", "metric_saturated_resource", "metric_anomaly_details",
    ])

    trace_available = state.get("trace_had_error", False)
    log_available = state.get("log_had_error", False)
    metric_available = state.get("metric_had_error", False)

    human_prompt = f"""CORRELATION TASK:

Incident ID: {state['incident_id']}
Incident: {state.get('trigger_description', '')}

SIGNAL AVAILABILITY:
- Trace Analysis: {"AVAILABLE" if trace_available else "UNAVAILABLE"}
- Log Analysis: {"AVAILABLE" if log_available else "UNAVAILABLE"}
- Metric Analysis: {"AVAILABLE" if metric_available else "UNAVAILABLE"}

TRACE:
{trace_context if trace_available else "Not available"}

LOG:
{log_context if log_available else "Not available"}

METRIC:
{metric_context if metric_available else "Not available"}

TRIAGE:
{format_state_for_prompt(state, ['triage_findings', 'triage_severity'])}

Build the causal chain from ALL available signals.
Search for similar past incidents.
Write your correlation summary."""

    llm = get_llm_with_tools(CORRELATION_TOOLS)
    messages = build_base_messages(SYSTEM_PROMPT, human_prompt)

    try:
        final_text, tool_calls = run_react_loop(
            llm_with_tools=llm,
            tools=CORRELATION_TOOLS,
            messages=messages,
            agent_name="correlation",
            max_iterations=5,
        )

        correlation_summary = _extract_summary(final_text)
        causal_chain = _extract_causal_chain(final_text)
        similar_incidents = _extract_similar_incidents(final_text)

        logger.info(f"[correlation] complete — causal chain steps: {len(causal_chain)}")

        return {
            "correlation_summary": correlation_summary,
            "causal_chain": causal_chain,
            "similar_incidents": similar_incidents,
            "completed_agents": safe_append(state.get("completed_agents", []), "correlation"),
        }

    except Exception as e:
        logger.error(f"[correlation] failed: {e}")
        return {
            "correlation_summary": f"Correlation analysis failed: {str(e)[:200]}",
            "causal_chain": [],
            "similar_incidents": [],
            "completed_agents": safe_append(state.get("completed_agents", []), "correlation"),
            "failed_agents": safe_append(state.get("failed_agents", []), "correlation"),
            "errors": safe_append(state.get("errors", []), f"[correlation] {type(e).__name__}: {str(e)[:200]}"),
        }


def _extract_summary(text: str) -> str:
    section = re.search(r"## Correlation Summary\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if section:
        return section.group(1).strip()
    return text[:1000]


def _extract_causal_chain(text: str) -> list:
    chain = []
    for match in re.finditer(r"STEP_\d+:\s*(.+?)$", text, re.MULTILINE):
        chain.append(match.group(1).strip())
    if not chain:
        section = re.search(r"## Causal Chain\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
        if section:
            for line in section.group(1).strip().split("\n"):
                line = line.strip().lstrip("- ").strip()
                if line:
                    chain.append(line)
    return chain


def _extract_similar_incidents(text: str) -> list:
    incidents = []
    for match in re.finditer(r"(INC-[\w-]+)", text):
        inc_id = match.group(1)
        if inc_id not in incidents:
            incidents.append(inc_id)
    return incidents
