import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import AIMessage
from agents.state.postmortem_state import initial_state
from agents.pipeline.graph import build_pipeline


TRIAGE_RESPONSE = """## Triage Findings
- service_b error_rate: 31.2% (anomalous)
- service_a error_rate: 2.1% (normal, excluded)

## Severity Assessment
SEVERITY: SEV2
REASON: service_b at 31.2% error rate exceeds SEV2 threshold

## Confirmed Affected Services
- service_b: 31.2% error rate

## Time Window
INCIDENT_START: 2025-01-15T14:03:15Z
ANALYSIS_WINDOW: 2025-01-15T14:03:15Z to 2025-01-15T14:13:15Z"""

TRACE_RESPONSE = """## Trace Analysis Findings
- service_b shows first error at 14:03:15Z with p99=4.1s
- service_a errors appear after service_b, suggesting cascade
- error type: connection pool exhausted in service_b

## Error Propagation
FIRST_ERROR_SERVICE: service_b
FIRST_ERROR_TIME: 2025-01-15T14:03:15Z
CASCADE_PATH: service_b -> service_a

## Latency Analysis
- service_b p50: 890ms, p99: 4.1s (significant spike)
- service_a p50: 120ms, p99: 800ms (slight elevation)

## Error Types
- "connection pool exhausted: max connections (100) reached"
- "context deadline exceeded" propagated to service_a"""

LOG_RESPONSE = """## Log Analysis Findings
- 150 ERROR entries in service_b in the last 10 minutes
- Dominant pattern: connection pool exhausted
- service_a shows downstream timeout errors

## Error Patterns
PATTERN_1: "connection pool exhausted: max connections (100) reached" - 120 occurrences - first seen: 2025-01-15T14:03:14Z
PATTERN_2: "context deadline exceeded" - 30 occurrences - first seen: 2025-01-15T14:03:17Z

## First Error Time
FIRST_ERROR_LOG: 2025-01-15T14:03:14Z

## Failure Mechanism
Based on log patterns, the likely failure mechanism is: Database connection pool exhaustion in service_b caused cascading timeouts to service_a."""

METRIC_RESPONSE = """## Metric Analysis Findings
- service_b db_connections at 100 (pool max) — SATURATED
- service_b error_rate at 31.2% (3.9x baseline)
- service_b p99 latency at 4.1s (8.2x baseline)
- service_b request rate normal (45.2 rps vs baseline 48.0)
- service_a metrics mildly elevated downstream

## Resource Saturation
SATURATED_RESOURCE: db_connections
SATURATION_DETAIL: Active connections at 100/100, pool completely exhausted starting at 14:03:15Z

## Anomaly Timeline
FIRST_ANOMALY: db_connections became anomalous at ~14:03:15Z — value: 100 (pool max)
SECOND_ANOMALY: error_rate at 14:03:15Z — value: 0.312 (3.9x avg)

## Traffic Pattern
TRAFFIC: normal — request rate 45.2 rps vs baseline 48.0 rps"""

CORRELATION_RESPONSE = """## Correlation Summary
The incident began with database connection pool exhaustion in service_b at approximately 14:03:14Z. All 100 connections were consumed, causing incoming requests to fail with connection refused errors. service_a, which depends on service_b, began experiencing cascading timeouts seconds later. The error rate on service_b spiked to 31.2%, and p99 latency increased to 4.1s. This matches a known pattern: INC-2024-11-03, which was also caused by connection pool exhaustion under normal traffic, suggesting a systemic pool sizing issue rather than a traffic spike.

## Causal Chain
STEP_1: 14:03:14Z — DB connection pool exhausted in service_b (metric)
STEP_2: 14:03:15Z — Error rate spikes on service_b to 31.2% (metric)
STEP_3: 14:03:15Z — service_b traces show connection pool errors (trace)
STEP_4: 14:03:17Z — service_a begins seeing deadline exceeded errors (trace/log)
STEP_5: 14:03:17Z — service_a p99 latency elevates to 800ms (metric)

## Similar Past Incidents
INC-2024-11-03 — Database connection pool exhaustion — similarity: 0.89

## Signal Completeness
SIGNALS_AVAILABLE: trace=yes log=yes metric=yes
CONFIDENCE_IMPACT: All three signals available and consistent — high confidence"""

ROOT_CAUSE_RESPONSE = """## Root Cause
ROOT_CAUSE: Database connection pool exhaustion on service_b due to pool size (100) being insufficient for the connection demand during normal operations

## Root Cause Explanation
The root cause is connection pool exhaustion on service_b, which is the FIRST failure condition. All 100 database connections were consumed by normal traffic levels (45 rps), causing new requests to fail immediately. This is not a symptom — it is the primary failure that triggered all downstream effects including cascading timeouts to service_a. The pool size limit of 100 connections is the bottleneck that, if prevented (increased), would have entirely avoided this incident.

## Confidence
CONFIDENCE: 0.85
CONFIDENCE_REASON: All three observability signals (traces, logs, metrics) agree. Causal chain is clear and consistent. A similar past incident (INC-2024-11-03) had the same root cause. Slight uncertainty due to normal traffic levels — root cause is capacity, not traffic.

## Contributing Factors
- Connection pool max size set to 100, insufficient for sustained request volume
- No connection pool monitoring or alerting in place
- No circuit breaker between service_a and service_b
- Similar incident INC-2024-11-03 did not result in pool size increase

## Alternative Hypotheses
ALT_1: Connection leak (some connections not returned to pool) — lower probability given clean pool behavior before saturation"""

POSTMORTEM_RESPONSE = """# Postmortem: Database Connection Pool Exhaustion on Service B

## Incident Summary
| Field | Value |
|-------|-------|
| Incident ID | TEST-E2E-001 |
| Severity | SEV2 |
| Status | Resolved |
| Detected At | 2025-01-15T14:03:22Z |
| Duration | ~10 minutes |
| Affected Services | service_b, service_a |
| Root Cause Confidence | 85% |

## Impact
Users experienced errors and significant latency degradation on requests routed through service_b, with cascading timeouts affecting service_a. Approximately 31% of requests to service_b failed during the incident window.

## Timeline
| Time | Event | Signal Source |
|------|-------|---------------|
| 14:03:14Z | DB connection pool on service_b reaches 100/100 | metric |
| 14:03:15Z | service_b error rate spikes to 31.2% | metric |
| 14:03:15Z | service_b traces show connection pool errors | trace |
| 14:03:17Z | service_a begins seeing deadline exceeded errors | trace/log |
| 14:03:17Z | service_a p99 latency elevates to 800ms | metric |

## Root Cause
Database connection pool exhaustion on service_b due to pool size (100) being insufficient for the connection demand during normal operations.

**Confidence:** 85%

All three observability signals (traces, logs, metrics) agree. The causal chain is clear: the connection pool was consumed by normal traffic levels, causing new requests to fail immediately. Traffic was not abnormally high (45 rps vs baseline 48 rps), confirming this is a capacity issue rather than a traffic spike.

## Contributing Factors
- Connection pool max size set to 100, insufficient for sustained request volume
- No connection pool monitoring or alerting in place
- No circuit breaker between service_a and service_b
- Similar incident INC-2024-11-03 did not result in pool size increase

## Blast Radius
**Affected:** service_b (all requests), service_a (cascading timeouts, partial degradation)
**Not Affected:** service_c (no dependency on service_b)

## Signal Analysis
### Distributed Traces
service_b showed first errors at 14:03:15Z with p99=4.1s. Cascade path: service_b -> service_a. Error type: connection pool exhausted.

### Logs
120 occurrences of "connection pool exhausted: max connections (100) reached" in service_b starting at 14:03:14Z. 30 occurrences of "context deadline exceeded" in service_a starting at 14:03:17Z.

### Metrics
db_connections at 100/100 (pool saturated). Error rate at 31.2% (3.9x baseline). p99 latency at 4.1s (8.2x baseline). Request rate normal at 45.2 rps.

## Similar Past Incidents
| Incident | Similarity | Root Cause | Resolution Time |
|----------|-----------|------------|-----------------|
| INC-2024-11-03 | 0.89 | DB connection pool exhaustion | 47 minutes |

## Action Items
### Immediate (< 24 hours)
- [ ] Increase service_b connection pool max from 100 to 250
- [ ] Add DB connection pool saturation alert at 80% utilization

### Short-term (< 1 week)
- [ ] Implement circuit breaker pattern between service_a and service_b
- [ ] Add real-time connection pool monitoring dashboard

### Long-term (< 1 month)
- [ ] Review and resize all service connection pools based on peak demand analysis
- [ ] Implement connection pool auto-scaling or dynamic sizing

## Data Gaps
All three observability signals were available during this incident.

---
*Report generated by Ghost Debugger at 2025-01-15T14:13:22Z*
*Signal completeness: full*"""


class FakeLLM:
    def __init__(self):
        self.responses = {
            (False, "triage"): TRIAGE_RESPONSE,
            (False, "trace_analyzer"): TRACE_RESPONSE,
            (False, "log_correlator"): LOG_RESPONSE,
            (False, "metric_reasoner"): METRIC_RESPONSE,
            (False, "correlation"): CORRELATION_RESPONSE,
            (True, "root_cause"): ROOT_CAUSE_RESPONSE,
            (True, "postmortem_writer"): POSTMORTEM_RESPONSE,
        }
        self.bind_tools = MagicMock()
        self.bind_tools.return_value = self

    def invoke(self, messages):
        agent_name = "unknown"
        for m in messages:
            if hasattr(m, "content") and isinstance(m.content, str):
                content = m.content
                if "TRIAGE AGENT" in content:
                    agent_name = "triage"
                elif "TRACE ANALYSIS AGENT" in content:
                    agent_name = "trace_analyzer"
                elif "LOG ANALYSIS AGENT" in content:
                    agent_name = "log_correlator"
                elif "METRIC ANALYSIS AGENT" in content:
                    agent_name = "metric_reasoner"
                elif "CORRELATION AGENT" in content:
                    agent_name = "correlation"
                elif "ROOT CAUSE AGENT" in content:
                    agent_name = "root_cause"
                elif "POSTMORTEM WRITER" in content:
                    agent_name = "postmortem_writer"
                break
        return AIMessage(
            content=self.responses.get((agent_name in ("root_cause", "postmortem_writer"), agent_name), "Mock response"),
            additional_kwargs={},
        )


@pytest.fixture(autouse=True)
def mock_llm():
    llm = FakeLLM()
    patches = [
        patch("agents.shared.llm.get_llm", return_value=llm),
        patch("agents.shared.llm.get_llm_with_tools", return_value=llm),
        patch("agents.triage.agent.get_llm_with_tools", return_value=llm),
        patch("agents.trace_analyzer.agent.get_llm_with_tools", return_value=llm),
        patch("agents.log_correlator.agent.get_llm_with_tools", return_value=llm),
        patch("agents.metric_reasoner.agent.get_llm_with_tools", return_value=llm),
        patch("agents.correlation.agent.get_llm_with_tools", return_value=llm),
        patch("agents.root_cause.agent.get_llm", return_value=llm),
        patch("agents.postmortem_writer.agent.get_llm", return_value=llm),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


def make_mock_trace():
    return {
        "trace_id": "abc123",
        "spans": [{
            "span_id": "s1", "parent_span_id": "", "operation_name": "GET /api",
            "service_name": "service_b", "start_time_us": 1000000,
            "duration_us": 4100000, "is_error": True,
            "error_message": "connection pool exhausted", "attributes": {},
        }],
        "services": ["service_b", "service_a"],
        "duration_us": 4100000, "has_error": True,
        "root_service": "service_a", "root_operation": "GET /api/process",
        "start_time": "2025-01-15T14:03:15Z",
    }


@pytest.fixture
def mock_querier():
    with patch("agents.tools.registry.get_querier") as mock:
        querier = MagicMock()

        querier.query_traces.return_value = [make_mock_trace()]
        querier.compute_trace_error_rate.return_value = {
            "total_traces": 100, "error_traces": 31,
            "error_rate": 0.31, "p50_duration_us": 890000,
            "p99_duration_us": 4100000, "service": "service_b",
        }

        base_ts = {
            "metric_name": "error_rate",
            "labels": {"service": "service_b"},
            "data_points": [],
            "min_value": 0.02, "max_value": 0.312, "avg_value": 0.08,
            "latest_value": 0.312, "is_anomalous": True,
            "anomaly_reason": "latest 0.312 is 3.9x average 0.08",
        }
        querier.query_error_rate.return_value = {**base_ts}
        querier.query_latency_p99.return_value = {
            **base_ts, "metric_name": "latency_p99",
            "latest_value": 4.1, "avg_value": 0.5, "max_value": 4.1, "min_value": 0.2,
            "anomaly_reason": "latest 4.1s is 8.2x average 0.5s",
        }
        querier.query_latency_p50.return_value = {
            **base_ts, "metric_name": "latency_p50",
            "latest_value": 0.89, "avg_value": 0.2, "max_value": 0.89, "min_value": 0.1,
            "is_anomalous": False, "anomaly_reason": "",
        }
        querier.query_request_rate.return_value = {
            **base_ts, "metric_name": "request_rate",
            "latest_value": 45.2, "avg_value": 48.0, "max_value": 55.0, "min_value": 40.0,
            "is_anomalous": False, "anomaly_reason": "",
        }
        querier.query_db_connections.return_value = {
            **base_ts, "metric_name": "db_connections_active",
            "latest_value": 100.0, "avg_value": 35.0, "max_value": 100.0,
            "anomaly_reason": "latest 100.0 is 2.9x average 35.0",
        }
        querier.query_memory_usage.return_value = {
            **base_ts, "metric_name": "memory_bytes",
            "latest_value": 256_000_000, "avg_value": 230_000_000, "max_value": 260_000_000,
            "is_anomalous": False, "anomaly_reason": "",
        }
        querier.query_goroutine_count.return_value = {
            **base_ts, "metric_name": "goroutine_count",
            "latest_value": 850, "avg_value": 400, "max_value": 900,
            "is_anomalous": False, "anomaly_reason": "",
        }
        querier.query_logs.return_value = [{
            "log_id": "log1", "service_name": "service_b", "level": "ERROR",
            "message": "connection pool exhausted: max connections (100) reached",
            "trace_id": "abc123", "span_id": "s1",
            "timestamp": "2025-01-15T14:03:14Z", "fields": {},
        }]
        querier.search_similar_incidents.return_value = [{
            "incident_id": "INC-2024-11-03",
            "title": "service_b database connection pool exhaustion",
            "root_cause": "DB pool exhausted during traffic spike",
            "affected_services": ["service_a", "service_b"],
            "severity": "SEV1", "occurred_at": "2024-11-03T14:03:00Z",
            "resolved_at": "2024-11-03T14:50:00Z",
            "time_to_resolve_minutes": 47,
            "postmortem_summary": "Connection pool (100) exhausted.",
            "action_items": ["Increase pool size", "Add circuit breaker"],
            "similarity_score": 0.89,
        }]
        querier.get_services.return_value = ["service_a", "service_b", "service_c"]

        querier.store_postmortem.return_value = "TEST-E2E-001"

        mock.return_value = querier
        yield querier


def test_full_pipeline_produces_postmortem(mock_querier):
    pipeline = build_pipeline(checkpoint_db=":memory:")

    state = initial_state(
        incident_id="TEST-E2E-001",
        trigger_type="error_rate",
        trigger_description="error_rate 31.2% exceeded 5% threshold on service_b for 60s",
        affected_services=["service_a", "service_b"],
        detected_at="2025-01-15T14:03:22Z",
        analysis_window_seconds=600,
    )

    config = {"configurable": {"thread_id": "TEST-E2E-001"}}
    result = pipeline.invoke(state, config=config)

    assert "postmortem_writer" in result["completed_agents"], (
        f"Pipeline did not complete. Completed: {result['completed_agents']}, Failed: {result['failed_agents']}"
    )

    report = result["postmortem_report"]
    assert report, "Postmortem report should not be empty"
    assert len(report) > 200, f"Report too short ({len(report)} chars)"
    assert "# Postmortem" in report, "Report should start with Postmortem header"

    assert result["root_cause"], "Root cause should not be empty"
    assert "connection pool" in result["root_cause"].lower()
    assert result["root_cause_confidence"] == 0.85

    assert result["triage_severity"] == "SEV2"

    assert len(result["completed_agents"]) >= 7, (
        f"Expected at least 7 completed agents, got {result['completed_agents']}"
    )

    print(f"\n{'='*60}")
    print(f"Pipeline completed successfully!")
    print(f"Severity: {result['triage_severity']}")
    print(f"Root cause: {result['root_cause'][:100]}")
    print(f"Confidence: {result['root_cause_confidence']:.0%}")
    print(f"Completed agents: {result['completed_agents']}")
    print(f"Failed agents: {result['failed_agents']}")
    print(f"Report length: {len(result['postmortem_report'])} chars")
    print(f"{'='*60}")
    print("\nPOSTMORTEM REPORT (first 1000 chars):")
    print(result["postmortem_report"][:1000])


def test_pipeline_handles_empty_affected_services(mock_querier):
    pipeline = build_pipeline(checkpoint_db=":memory:")

    state = initial_state(
        incident_id="TEST-EMPTY",
        trigger_type="error_rate",
        trigger_description="Unknown error spike detected",
        affected_services=[],
        detected_at="2025-01-15T14:03:22Z",
        analysis_window_seconds=600,
    )

    config = {"configurable": {"thread_id": "TEST-EMPTY"}}
    result = pipeline.invoke(state, config=config)

    assert "postmortem_writer" in result["completed_agents"]
    assert result["postmortem_report"]
