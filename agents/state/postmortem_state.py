import operator
from typing import TypedDict, List, Annotated


class PostmortemState(TypedDict):
    incident_id: str
    trigger_type: str
    trigger_description: str
    affected_services: List[str]
    detected_at: str
    analysis_window_seconds: int

    triage_findings: List[str]
    triage_severity: str
    triage_time_window: str
    triage_confirmed_services: List[str]

    trace_findings: List[str]
    trace_first_error_service: str
    trace_first_error_time: str
    trace_cascade_path: List[str]
    trace_had_error: bool

    log_findings: List[str]
    log_error_patterns: List[str]
    log_first_error_time: str
    log_had_error: bool

    metric_findings: List[str]
    metric_saturated_resource: str
    metric_anomaly_details: List[str]
    metric_had_error: bool

    correlation_summary: str
    causal_chain: List[str]
    similar_incidents: List[str]

    root_cause: str
    root_cause_confidence: float
    contributing_factors: List[str]

    postmortem_report: str
    signal_completeness: str

    completed_agents: Annotated[List[str], operator.add]
    failed_agents: Annotated[List[str], operator.add]
    errors: Annotated[List[str], operator.add]
    analysis_start_at: str
    analysis_end_at: str


def initial_state(
    incident_id: str,
    trigger_type: str,
    trigger_description: str,
    affected_services: List[str],
    detected_at: str,
    analysis_window_seconds: int = 600,
) -> PostmortemState:
    return PostmortemState(
        incident_id=incident_id,
        trigger_type=trigger_type,
        trigger_description=trigger_description,
        affected_services=list(affected_services),
        detected_at=detected_at,
        analysis_window_seconds=analysis_window_seconds,
        triage_findings=[],
        triage_severity="UNKNOWN",
        triage_time_window="",
        triage_confirmed_services=[],
        trace_findings=[],
        trace_first_error_service="",
        trace_first_error_time="",
        trace_cascade_path=[],
        trace_had_error=False,
        log_findings=[],
        log_error_patterns=[],
        log_first_error_time="",
        log_had_error=False,
        metric_findings=[],
        metric_saturated_resource="",
        metric_anomaly_details=[],
        metric_had_error=False,
        correlation_summary="",
        causal_chain=[],
        similar_incidents=[],
        root_cause="",
        root_cause_confidence=0.0,
        contributing_factors=[],
        postmortem_report="",
        signal_completeness="full",
        completed_agents=[],
        failed_agents=[],
        errors=[],
        analysis_start_at="",
        analysis_end_at="",
    )
