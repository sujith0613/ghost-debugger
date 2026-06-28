# agents/tools/telemetry_querier.py
#
# LangChain tools that agents use to query observability backends.
#
# DESIGN DECISION:
# All storage queries go through this module — agents never call
# Jaeger/Prometheus/ChromaDB directly.

from langchain_core.tools import tool
import json


@tool
def query_traces(service_name: str, lookback_minutes: int) -> str:
    """
    Query distributed traces from Jaeger for a specific service.
    Returns trace summary including error rate, p50/p99 latency, error types,
    and the timestamp when errors first appeared.

    Args:
        service_name: The service to query (e.g., 'service_a', 'service_b')
        lookback_minutes: How many minutes back to search (e.g., 15)
    """
    return json.dumps({"status": "stub — implement in Phase 2", "service": service_name})


@tool
def query_metrics(metric_name: str, service_name: str, lookback_minutes: int) -> str:
    """
    Query Prometheus for time-series metric data.
    Available metrics: 'error_rate', 'request_rate', 'cpu_usage_percent',
    'memory_bytes', 'goroutine_count', 'db_connections_active',
    'db_connections_max', 'p99_latency_ms', 'p50_latency_ms'

    Args:
        metric_name: Which metric to query (see available metrics above)
        service_name: The service to query metrics for
        lookback_minutes: Time range in minutes (e.g., 15)
    """
    return json.dumps({"status": "stub — implement in Phase 2", "metric": metric_name})


@tool
def query_logs(service_name: str, level: str, lookback_minutes: int) -> str:
    """
    Query structured logs for a specific service filtered by log level.
    Returns log clusters (grouped similar messages), first occurrence time,
    and count of each error type.

    Args:
        service_name: The service to query logs for
        level: Log level filter — 'ERROR', 'WARN', or 'INFO'
        lookback_minutes: How far back to search
    """
    return json.dumps({"status": "stub — implement in Phase 2", "service": service_name})


@tool
def search_similar_incidents(incident_description: str) -> str:
    """
    Search the historical incident database for past incidents similar
    to the current one.

    Args:
        incident_description: Description of current incident characteristics
    """
    return json.dumps({"status": "stub — implement in Phase 2"})
