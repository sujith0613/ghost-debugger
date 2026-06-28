import json
import logging
import os
import re
from functools import lru_cache
from langchain_core.tools import tool
from agents.storage.querier import TelemetryQuerier
from agents.storage.base import QueryError

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_querier() -> TelemetryQuerier:
    return TelemetryQuerier(
        jaeger_url=os.getenv("JAEGER_URL", "http://localhost:16686"),
        prometheus_url=os.getenv("PROMETHEUS_URL", "http://localhost:9090"),
        chromadb_host=os.getenv("CHROMADB_HOST", "localhost"),
        chromadb_port=int(os.getenv("CHROMADB_PORT", "8000")),
        seed_chromadb=True,
    )


def _error_response(tool_name: str, error: Exception) -> str:
    return json.dumps({
        "error": True, "tool": tool_name, "message": str(error), "data": None,
    })


# -- Trace tools -------------------------------------------------------------��──────────────────────────────────

@tool
def tool_query_traces(
    service_name: str,
    lookback_minutes: int = 60,
    only_errors: bool = False,
) -> str:
    """Query Jaeger traces for a service.
    Use for: finding cascade path, first-error service, error spans,
    service dependencies, trace-level latency.
    Do NOT use for: log messages, metric aggregates, RAG.
    """
    try:
        querier = get_querier()
        traces = querier.query_traces(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
            only_errors=only_errors,
            limit=100,
        )

        if not traces:
            return json.dumps({
                "service": service_name, "total_traces": 0,
                "message": f"No traces found for {service_name} in last {lookback_minutes} minutes",
            })

        total = len(traces)
        error_count = sum(1 for t in traces if t["has_error"])
        error_rate = error_count / total if total > 0 else 0.0

        durations_ms = sorted(t["duration_us"] / 1000 for t in traces)
        p50_idx = int(len(durations_ms) * 0.50)
        p99_idx = int(len(durations_ms) * 0.99)
        p50_ms = round(durations_ms[p50_idx], 1) if durations_ms else 0
        p99_ms = round(durations_ms[min(p99_idx, len(durations_ms) - 1)], 1) if durations_ms else 0

        error_traces = [t for t in traces if t["has_error"]]
        first_error_time = ""
        if error_traces:
            first_error_time = min(t["start_time"] for t in error_traces if t["start_time"])

        all_services = set()
        for t in traces:
            all_services.update(t["services"])

        sample_errors = []
        for t in error_traces[:5]:
            for span in t["spans"]:
                if span["is_error"] and span["error_message"]:
                    sample_errors.append({
                        "service": span["service_name"],
                        "operation": span["operation_name"],
                        "error": span["error_message"][:200],
                    })

        result = {
            "service": service_name,
            "lookback_minutes": lookback_minutes,
            "total_traces": total,
            "error_traces": error_count,
            "error_rate": round(error_rate, 4),
            "error_rate_percent": round(error_rate * 100, 2),
            "p50_latency_ms": p50_ms,
            "p99_latency_ms": p99_ms,
            "first_error_time": first_error_time,
            "services_seen_in_traces": sorted(all_services),
            "sample_errors": sample_errors[:5],
        }

        return json.dumps(result, indent=2)

    except QueryError as e:
        return _error_response("tool_query_traces", e)
    except Exception as e:
        return _error_response("tool_query_traces", e)


@tool
def tool_get_trace_error_rate(service_name: str, lookback_minutes: int = 60) -> str:
    """Get the error rate of traces for a service.
    Use for: quick error-rate check without full trace details.
    """
    try:
        querier = get_querier()
        stats = querier.compute_trace_error_rate(service_name, lookback_minutes)
        stats["service"] = service_name
        return json.dumps(stats, indent=2)
    except QueryError as e:
        return _error_response("tool_get_trace_error_rate", e)
    except Exception as e:
        return _error_response("tool_get_trace_error_rate", e)


# -- Metric tools ------------------------------------------------------------��──────────────────────────────────

@tool
def tool_query_error_rate(service_name: str, lookback_minutes: int = 60) -> str:
    """Query Prometheus error_rate for a service.
    Use for: detecting elevated errors, anomaly scoring.
    """
    try:
        querier = get_querier()
        ts = querier.query_error_rate(service_name, lookback_minutes)
        return json.dumps({
            "service": service_name, "metric": "error_rate",
            "latest_value": ts["latest_value"],
            "latest_percent": round(ts["latest_value"] * 100, 2),
            "avg_value": ts["avg_value"], "max_value": ts["max_value"],
            "min_value": ts["min_value"],
            "is_anomalous": ts["is_anomalous"],
            "anomaly_reason": ts["anomaly_reason"],
            "data_point_count": len(ts["data_points"]),
            "lookback_minutes": lookback_minutes,
        }, indent=2)
    except QueryError as e:
        return _error_response("tool_query_error_rate", e)
    except Exception as e:
        return _error_response("tool_query_error_rate", e)


@tool
def tool_query_latency_p99(service_name: str, lookback_minutes: int = 60) -> str:
    """Query p99 latency for a service from Prometheus.
    Use for: detecting latency spikes, service degradation.
    """
    try:
        querier = get_querier()
        ts = querier.query_latency_p99(service_name, lookback_minutes)
        return json.dumps({
            "service": service_name, "metric": "latency_p99",
            "latest_value_seconds": ts["latest_value"],
            "latest_value_ms": round(ts["latest_value"] * 1000, 1),
            "avg_ms": round(ts["avg_value"] * 1000, 1),
            "max_ms": round(ts["max_value"] * 1000, 1),
            "is_anomalous": ts["is_anomalous"],
            "anomaly_reason": ts["anomaly_reason"],
            "lookback_minutes": lookback_minutes,
        }, indent=2)
    except QueryError as e:
        return _error_response("tool_query_latency_p99", e)
    except Exception as e:
        return _error_response("tool_query_latency_p99", e)


@tool
def tool_query_latency_p50(service_name: str, lookback_minutes: int = 60) -> str:
    """Query p50 latency for a service from Prometheus.
    Use for: baseline comparison against p99 to spot tail latency.
    """
    try:
        querier = get_querier()
        ts = querier.query_latency_p50(service_name, lookback_minutes)
        return json.dumps({
            "service": service_name, "metric": "latency_p50",
            "latest_value_ms": round(ts["latest_value"] * 1000, 1),
            "avg_ms": round(ts["avg_value"] * 1000, 1),
            "max_ms": round(ts["max_value"] * 1000, 1),
            "is_anomalous": ts["is_anomalous"],
            "anomaly_reason": ts["anomaly_reason"],
            "lookback_minutes": lookback_minutes,
        }, indent=2)
    except QueryError as e:
        return _error_response("tool_query_latency_p50", e)
    except Exception as e:
        return _error_response("tool_query_latency_p50", e)


@tool
def tool_query_request_rate(service_name: str, lookback_minutes: int = 60) -> str:
    """Query request rate (RPS) for a service from Prometheus.
    Use for: traffic spike detection, load correlation.
    """
    try:
        querier = get_querier()
        ts = querier.query_request_rate(service_name, lookback_minutes)
        return json.dumps({
            "service": service_name, "metric": "request_rate",
            "latest_rps": round(ts["latest_value"], 3),
            "avg_rps": round(ts["avg_value"], 3),
            "max_rps": round(ts["max_value"], 3),
            "min_rps": round(ts["min_value"], 3),
            "is_anomalous": ts["is_anomalous"],
            "anomaly_reason": ts["anomaly_reason"],
            "lookback_minutes": lookback_minutes,
        }, indent=2)
    except QueryError as e:
        return _error_response("tool_query_request_rate", e)
    except Exception as e:
        return _error_response("tool_query_request_rate", e)


@tool
def tool_query_db_connections(service_name: str, lookback_minutes: int = 60) -> str:
    """Query active DB connections for a service from Prometheus.
    Use for: detecting connection pool exhaustion.
    """
    try:
        querier = get_querier()
        ts = querier.query_db_connections(service_name, lookback_minutes)
        return json.dumps({
            "service": service_name, "metric": "db_connections_active",
            "latest_active": ts["latest_value"],
            "avg_active": ts["avg_value"],
            "max_active": ts["max_value"],
            "is_anomalous": ts["is_anomalous"],
            "anomaly_reason": ts["anomaly_reason"],
            "interpretation": (
                "POOL EXHAUSTED"
                if ts["is_anomalous"] else "Connection pool within normal range"
            ),
            "lookback_minutes": lookback_minutes,
        }, indent=2)
    except QueryError as e:
        return _error_response("tool_query_db_connections", e)
    except Exception as e:
        return _error_response("tool_query_db_connections", e)


@tool
def tool_query_memory_usage(service_name: str, lookback_minutes: int = 60) -> str:
    """Query memory usage (bytes) for a service from Prometheus.
    Use for: detecting memory leaks, OOM risk.
    """
    try:
        querier = get_querier()
        ts = querier.query_memory_usage(service_name, lookback_minutes)
        return json.dumps({
            "service": service_name, "metric": "memory_bytes",
            "latest_mb": round(ts["latest_value"] / (1024 * 1024), 1),
            "avg_mb": round(ts["avg_value"] / (1024 * 1024), 1),
            "max_mb": round(ts["max_value"] / (1024 * 1024), 1),
            "is_anomalous": ts["is_anomalous"],
            "anomaly_reason": ts["anomaly_reason"],
            "lookback_minutes": lookback_minutes,
        }, indent=2)
    except QueryError as e:
        return _error_response("tool_query_memory_usage", e)
    except Exception as e:
        return _error_response("tool_query_memory_usage", e)


@tool
def tool_query_goroutine_count(service_name: str, lookback_minutes: int = 60) -> str:
    """Query goroutine count for a service from Prometheus.
    Use for: detecting goroutine leaks, abnormal concurrency.
    """
    try:
        querier = get_querier()
        ts = querier.query_goroutine_count(service_name, lookback_minutes)
        latest = int(ts["latest_value"])
        return json.dumps({
            "service": service_name, "metric": "goroutine_count",
            "latest_goroutines": latest,
            "avg_goroutines": int(ts["avg_value"]),
            "max_goroutines": int(ts["max_value"]),
            "is_anomalous": ts["is_anomalous"],
            "anomaly_reason": ts["anomaly_reason"],
            "interpretation": (
                "GOROUTINE LEAK"
                if latest > 10_000 else "Goroutine count within normal range"
            ),
            "lookback_minutes": lookback_minutes,
        }, indent=2)
    except QueryError as e:
        return _error_response("tool_query_goroutine_count", e)
    except Exception as e:
        return _error_response("tool_query_goroutine_count", e)


# -- Log tools ---------------------------------------------------------------��─────────────────────────────────────

@tool
def tool_query_logs(
    service_name: str,
    level: str = "ERROR",
    lookback_minutes: int = 60,
) -> str:
    """Query logs for a service from Loki.
    Use for: finding error messages, pattern clustering, timestamps.
    Do NOT use for: trace information, metric values, RAG.
    """
    try:
        querier = get_querier()
        entries = querier.query_logs(
            service_name=service_name,
            level=level,
            lookback_minutes=lookback_minutes,
            limit=200,
        )

        if not entries:
            return json.dumps({
                "service": service_name, "level": level, "total_entries": 0,
                "message": f"No {level} logs found for {service_name}",
            })

        pattern_counts = {}
        for entry in entries:
            msg = entry["message"][:100]
            key = re.sub(r'\b\d+\b', 'N', msg)
            key = re.sub(r'[0-9a-f]{8}-[0-9a-f-]+', 'UUID', key)
            pattern_counts[key] = pattern_counts.get(key, 0) + 1

        sorted_patterns = sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True)

        timestamps = [e["timestamp"] for e in entries if e["timestamp"]]
        first_time = min(timestamps) if timestamps else ""

        result = {
            "service": service_name, "level": level,
            "total_entries": len(entries),
            "first_error_time": first_time,
            "distinct_patterns": len(sorted_patterns),
            "top_error_patterns": [
                {"pattern": p, "count": c}
                for p, c in sorted_patterns[:10]
            ],
            "sample_messages": [
                {
                    "timestamp": e["timestamp"],
                    "message": e["message"][:300],
                    "trace_id": e["trace_id"],
                }
                for e in entries[:10]
            ],
        }

        return json.dumps(result, indent=2)

    except QueryError as e:
        return _error_response("tool_query_logs", e)
    except Exception as e:
        return _error_response("tool_query_logs", e)


# -- RAG tool ----------------------------------------------------------------��──────────────────────────────────────

@tool
def tool_search_similar_incidents(
    incident_description: str,
    n_results: int = 3,
) -> str:
    """Search past incidents by semantic similarity via ChromaDB.
    Use for: finding similar past incidents, known patterns, historical fixes.
    Do NOT use for: current live data queries (traces, metrics, logs).
    """
    try:
        querier = get_querier()
        incidents = querier.search_similar_incidents(
            query_text=incident_description,
            n_results=min(n_results, 5),
            min_similarity=0.2,
        )

        if not incidents:
            return json.dumps({
                "similar_incidents_found": 0,
                "message": "No similar past incidents found.",
            })

        result = {
            "similar_incidents_found": len(incidents),
            "incidents": [
                {
                    "incident_id": inc["incident_id"],
                    "title": inc["title"],
                    "similarity_score": inc["similarity_score"],
                    "severity": inc["severity"],
                    "root_cause": inc["root_cause"],
                    "affected_services": inc["affected_services"],
                    "time_to_resolve_minutes": inc["time_to_resolve_minutes"],
                    "postmortem_summary": inc["postmortem_summary"][:500],
                    "top_action_items": inc["action_items"][:3],
                }
                for inc in incidents
            ],
        }

        return json.dumps(result, indent=2)

    except QueryError as e:
        return _error_response("tool_search_similar_incidents", e)
    except Exception as e:
        return _error_response("tool_search_similar_incidents", e)


# -- Tool sets for each agent ------------------------------------------------��──────────────────────

TRIAGE_TOOLS = [
    tool_query_error_rate,
    tool_query_request_rate,
    tool_get_trace_error_rate,
    tool_query_latency_p99,
]

TRACE_ANALYZER_TOOLS = [
    tool_query_traces,
    tool_get_trace_error_rate,
    tool_query_latency_p99,
    tool_query_latency_p50,
]

LOG_CORRELATOR_TOOLS = [
    tool_query_logs,
]

METRIC_REASONER_TOOLS = [
    tool_query_error_rate,
    tool_query_latency_p99,
    tool_query_latency_p50,
    tool_query_request_rate,
    tool_query_db_connections,
    tool_query_memory_usage,
    tool_query_goroutine_count,
]

CORRELATION_TOOLS = [
    tool_search_similar_incidents,
]

ROOT_CAUSE_TOOLS = []
POSTMORTEM_WRITER_TOOLS = []
