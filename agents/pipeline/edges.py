import logging
from typing import List, Union
from langgraph.types import Send
from agents.state.postmortem_state import PostmortemState

logger = logging.getLogger(__name__)


def route_after_triage(state: PostmortemState) -> str:
    confirmed = state.get("triage_confirmed_services", [])
    severity = state.get("triage_severity", "UNKNOWN")

    if not confirmed:
        logger.info(f"[route] no confirmed services -> postmortem_writer (false positive)")
        return "postmortem_writer"

    if severity == "SEV3":
        logger.info(f"[route] SEV3 -> correlation fast path")
        return "correlation"

    logger.info(f"[route] {severity} -> parallel_analysis")
    return "parallel_analysis"


def fan_out_to_parallel_agents(state: PostmortemState) -> List[Send]:
    logger.info(f"[fan_out] dispatching to 3 parallel agents")
    return [
        Send("trace_analyzer", state),
        Send("log_correlator", state),
        Send("metric_reasoner", state),
    ]


def route_after_postmortem(state: PostmortemState) -> str:
    report = state.get("postmortem_report", "")
    confidence = state.get("root_cause_confidence", 0.0)
    has_analysis = any([
        state.get("trace_had_error"),
        state.get("log_had_error"),
        state.get("metric_had_error"),
    ])

    if len(report) > 200 and confidence >= 0.5 and has_analysis:
        logger.info(f"[route] postmortem qualifies for ChromaDB storage (confidence={confidence:.2f})")
        return "store_postmortem"

    logger.info(f"[route] postmortem below quality threshold - skipping storage "
                f"(len={len(report)}, confidence={confidence:.2f}, has_analysis={has_analysis})")
    return "end"
