from typing import TypedDict, List, Optional, Dict, Any
from datetime import datetime


class Span(TypedDict):
    span_id: str
    parent_span_id: str
    operation_name: str
    service_name: str
    start_time_us: int
    duration_us: int
    is_error: bool
    error_message: str
    attributes: Dict[str, str]


class Trace(TypedDict):
    trace_id: str
    spans: List[Span]
    services: List[str]
    duration_us: int
    has_error: bool
    root_service: str
    root_operation: str
    start_time: str


class DataPoint(TypedDict):
    timestamp: float
    value: float


class TimeSeries(TypedDict):
    metric_name: str
    labels: Dict[str, str]
    data_points: List[DataPoint]
    min_value: float
    max_value: float
    avg_value: float
    latest_value: float
    is_anomalous: bool
    anomaly_reason: str


class LogEntry(TypedDict):
    log_id: str
    service_name: str
    level: str
    message: str
    trace_id: str
    span_id: str
    timestamp: str
    fields: Dict[str, str]


class PastIncident(TypedDict):
    incident_id: str
    title: str
    root_cause: str
    affected_services: List[str]
    severity: str
    occurred_at: str
    resolved_at: str
    time_to_resolve_minutes: int
    postmortem_summary: str
    action_items: List[str]
    similarity_score: float


class QueryError(Exception):
    def __init__(self, backend: str, query: str, cause: Exception):
        self.backend = backend
        self.query = query
        self.cause = cause
        super().__init__(
            f"[{backend}] query failed: '{query}' — {type(cause).__name__}: {cause}"
        )
