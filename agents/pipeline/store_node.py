import logging
from datetime import datetime, timezone
from agents.state.postmortem_state import PostmortemState
from agents.tools.registry import get_querier

logger = logging.getLogger(__name__)


def store_postmortem_node(state: PostmortemState) -> dict:
    incident_id = state["incident_id"]
    logger.info(f"[store_postmortem] storing postmortem for {incident_id}")

    try:
        querier = get_querier()

        summary_parts = []
        if state.get("root_cause"):
            summary_parts.append(f"Root cause: {state['root_cause']}")
        if state.get("correlation_summary"):
            summary_parts.append(state["correlation_summary"][:500])
        if state.get("metric_findings"):
            summary_parts.extend(state["metric_findings"][:3])
        if state.get("log_error_patterns"):
            summary_parts.extend(state["log_error_patterns"][:2])

        postmortem_summary = "\n".join(summary_parts) if summary_parts else \
            state.get("trigger_description", "")

        resolved_at = state.get("analysis_end_at") or datetime.now(tz=timezone.utc).isoformat()

        try:
            start = datetime.fromisoformat(state.get("analysis_start_at", resolved_at).replace("Z", "+00:00"))
            end = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
            ttm_minutes = max(1, int((end - start).total_seconds() / 60))
        except Exception:
            ttm_minutes = 0

        action_items = []
        for factor in state.get("contributing_factors", []):
            if factor and factor != "Not determined":
                action_items.append(f"Address: {factor}")

        if not action_items:
            action_items = ["Review incident timeline", "Update runbook"]

        stored_id = querier.store_postmortem(
            incident_id=incident_id,
            title=f"Incident: {state.get('trigger_description', incident_id)[:100]}",
            root_cause=state.get("root_cause", "Unknown"),
            affected_services=state.get("triage_confirmed_services") or
                              state.get("affected_services", []),
            severity=state.get("triage_severity", "UNKNOWN"),
            occurred_at=state.get("detected_at", datetime.now(tz=timezone.utc).isoformat()),
            resolved_at=resolved_at,
            time_to_resolve_minutes=ttm_minutes,
            postmortem_summary=postmortem_summary,
            action_items=action_items,
            postmortem_full_markdown=state.get("postmortem_report", ""),
        )

        logger.info(f"[store_postmortem] stored successfully: {stored_id}")

        return {
            "completed_agents": ["store_postmortem"],
        }

    except Exception as e:
        logger.error(f"[store_postmortem] failed: {e}")
        return {
            "errors": [f"[store_postmortem] ChromaDB storage failed: {str(e)[:200]}"],
        }
