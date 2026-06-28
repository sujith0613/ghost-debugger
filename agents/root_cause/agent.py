import logging
import re
from agents.state.postmortem_state import PostmortemState
from agents.shared.llm import get_llm
from agents.shared.node_utils import build_base_messages, safe_append, format_state_for_prompt

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the ROOT CAUSE AGENT in an automated incident analysis system.

You receive a complete correlation analysis and determine the DEFINITIVE ROOT CAUSE.
You have NO tools — you reason purely from the evidence provided.

ROOT CAUSE DEFINITION:
The root cause is the FIRST failure condition that, if prevented, would have
prevented the entire incident. It is NOT a symptom.

CONFIDENCE SCORING:
- 0.90-1.00: All three signals agree + matches past incident with same root cause
- 0.75-0.89: Two signals agree + consistent narrative
- 0.60-0.74: One signal available or signals are ambiguous
- 0.40-0.59: Insufficient data, educated guess
- 0.00-0.39: Highly uncertain, multiple conflicting hypotheses

CONTRIBUTING FACTORS:
Conditions that made the root cause possible.

OUTPUT FORMAT:

## Root Cause
ROOT_CAUSE: [concise one-sentence root cause statement]

## Root Cause Explanation
[2-3 sentences explaining why this is the root cause, not a symptom]

## Confidence
CONFIDENCE: [0.00-1.00]
CONFIDENCE_REASON: [why this confidence level]

## Contributing Factors
- [factor 1]
- [factor 2]
- [factor 3]

## Alternative Hypotheses
(Only if confidence < 0.75)
ALT_1: [alternative explanation with probability]"""


def root_cause_agent_node(state: PostmortemState) -> dict:
    logger.info(f"[root_cause] starting — incident: {state['incident_id']}")

    all_context = format_state_for_prompt(state, [
        "triage_findings", "triage_severity",
        "trace_findings", "trace_first_error_service",
        "trace_first_error_time", "trace_cascade_path",
        "log_findings", "log_error_patterns", "log_first_error_time",
        "metric_findings", "metric_saturated_resource", "metric_anomaly_details",
        "correlation_summary", "causal_chain", "similar_incidents",
    ])

    human_prompt = f"""ROOT CAUSE DETERMINATION TASK:

Incident ID: {state['incident_id']}
Severity: {state.get('triage_severity', 'UNKNOWN')}

ALL EVIDENCE:
{all_context}

Based on ALL the evidence above, determine the definitive root cause.
Remember: root cause = first failure condition, not a symptom.
Assign a confidence score and list contributing factors."""

    llm = get_llm()
    messages = build_base_messages(SYSTEM_PROMPT, human_prompt)

    try:
        response = llm.invoke(messages)
        final_text = response.content or ""

        root_cause = _extract_root_cause(final_text)
        confidence = _extract_confidence(final_text)
        contributing_factors = _extract_contributing_factors(final_text)

        logger.info(f"[root_cause] complete — confidence={confidence:.2f}")

        return {
            "root_cause": root_cause,
            "root_cause_confidence": confidence,
            "contributing_factors": contributing_factors,
            "completed_agents": safe_append(state.get("completed_agents", []), "root_cause"),
        }

    except Exception as e:
        logger.error(f"[root_cause] failed: {e}")
        fallback_rc = (
            state.get("metric_saturated_resource") or
            state.get("correlation_summary", "Unknown — analysis failed")[:200]
        )
        return {
            "root_cause": fallback_rc,
            "root_cause_confidence": 0.3,
            "contributing_factors": ["Root cause analysis incomplete due to error"],
            "completed_agents": safe_append(state.get("completed_agents", []), "root_cause"),
            "failed_agents": safe_append(state.get("failed_agents", []), "root_cause"),
            "errors": safe_append(state.get("errors", []), f"[root_cause] {type(e).__name__}: {str(e)[:200]}"),
        }


def _extract_root_cause(text: str) -> str:
    match = re.search(r"ROOT_CAUSE:\s*(.+?)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    section = re.search(r"## Root Cause\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if section:
        lines = [l.strip() for l in section.group(1).strip().split("\n") if l.strip()]
        if lines:
            return lines[0].lstrip("- ").strip()
    return "Root cause could not be determined"


def _extract_confidence(text: str) -> float:
    match = re.search(r"CONFIDENCE:\s*([\d.]+)", text, re.IGNORECASE)
    if match:
        try:
            val = float(match.group(1))
            return max(0.0, min(1.0, val))
        except ValueError:
            pass
    return 0.5


def _extract_contributing_factors(text: str) -> list:
    factors = []
    section = re.search(r"## Contributing Factors\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if section:
        for line in section.group(1).strip().split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line:
                factors.append(line)
    return factors or ["Contributing factors not determined"]
