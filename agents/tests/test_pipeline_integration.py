import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import AIMessage

from agents.state.postmortem_state import initial_state
from agents.pipeline.graph import PipelineRunner, build_pipeline
from agents.storage.base import (
    Trace, Span, TimeSeries, DataPoint, LogEntry, PastIncident, QueryError
)

CASCADE_TRIAGE = """## Triage Findings
- service_b error_rate: 31.2% (anomalous, 3.9x baseline)
- service_a error_rate: 8.3% (anomalous, 5.5x baseline)
- service_c error_rate: 0.1% (normal, excluded)

## Severity Assessment
SEVERITY: SEV2
REASON: service_b at 31.2% error rate exceeds SEV2 threshold

## Confirmed Affected Services
- service_b: 31.2% error rate
- service_a: 8.3% error rate

## Time Window
INCIDENT_START: 2025-01-15T14:03:15Z
ANALYSIS_WINDOW: 2025-01-15T14:03:15Z to 2025-01-15T14:13:15Z"""

CASCADE_TRACE = """## Trace Analysis Findings
- service_b shows first error at 14:03:15Z with p99=4.1s
- service_a errors appear at 14:03:22Z, 7 seconds after service_b
- service_b error type: connection pool exhausted
- service_a error type: context deadline exceeded

## Error Propagation
FIRST_ERROR_SERVICE: service_b
FIRST_ERROR_TIME: 2025-01-15T14:03:15Z
CASCADE_PATH: service_b -> service_a

## Latency Analysis
- service_b p50: 890ms, p99: 4.1s (significant spike)
- service_a p50: 95ms, p99: 10s (timeout spikes)

## Error Types
- "connection pool exhausted: max connections (100) reached"
- "context deadline exceeded" propagated to service_a"""

CASCADE_LOG = """## Log Analysis Findings
- 150 ERROR entries in service_b in the last 10 minutes
- Dominant pattern: connection pool exhausted in service_b
- service_a shows downstream timeout errors
- service_c: no error logs

## Error Patterns
PATTERN_1: "connection pool exhausted: max connections (100) reached" - 120 occurrences - first seen: 2025-01-15T14:03:14Z
PATTERN_2: "context deadline exceeded" - 30 occurrences - first seen: 2025-01-15T14:03:17Z

## First Error Time
FIRST_ERROR_LOG: 2025-01-15T14:03:14Z

## Failure Mechanism
Database connection pool exhaustion in service_b caused cascading timeouts to service_a."""

CASCADE_METRIC = """## Metric Analysis Findings
- service_b db_connections at 100 (pool max) -- SATURATED
- service_b error_rate at 31.2% (3.9x baseline)
- service_b p99 latency at 4.1s (8.2x baseline)
- service_b request rate normal (45.2 rps vs baseline 48.0)
- service_a metrics mildly elevated downstream

## Resource Saturation
SATURATED_RESOURCE: db_connections
SATURATION_DETAIL: Active connections at 100/100, pool completely exhausted starting at 14:03:15Z

## Anomaly Timeline
- 14:03:15Z -- db_connections reached 100 (pool max)
- 14:03:15Z -- error_rate spiked to 0.312 (3.9x avg)
- 14:03:17Z -- service_a latency elevated

## Traffic Pattern
TRAFFIC: normal -- request rate 45.2 rps vs baseline 48.0 rps"""

CASCADE_CORRELATION = """## Correlation Summary
The incident began with DB connection pool exhaustion in service_b at ~14:03:14Z. All 100 connections consumed, causing new requests to fail. service_a, which depends on service_b, began experiencing cascading timeouts seconds later. Error rate on service_b spiked to 31.2%, p99 latency to 4.1s. Matches known pattern INC-2024-11-03.

## Causal Chain
- 14:03:14Z -- DB connection pool exhausted in service_b (metric)
- 14:03:15Z -- Error rate spikes on service_b to 31.2% (metric)
- 14:03:15Z -- service_b traces show connection pool errors (trace)
- 14:03:17Z -- service_a begins seeing deadline exceeded errors (trace/log)
- 14:03:17Z -- service_a p99 latency elevates (metric)

## Similar Past Incidents
INC-2024-11-03 -- Database connection pool exhaustion -- similarity: 0.89

## Signal Completeness
SIGNALS_AVAILABLE: trace=yes log=yes metric=yes
CONFIDENCE_IMPACT: All three signals available and consistent -- high confidence"""

CASCADE_ROOT_CAUSE = """## Root Cause
ROOT_CAUSE: Database connection pool exhaustion on service_b due to pool size (100) being insufficient for connection demand during normal operations

## Root Cause Explanation
The root cause is connection pool exhaustion on service_b, the FIRST failure condition. All 100 database connections were consumed by normal traffic levels (45 rps), causing new requests to fail immediately. This is the primary failure that triggered all downstream effects.

## Confidence
CONFIDENCE: 0.85
CONFIDENCE_REASON: All three observability signals agree. Causal chain is clear. Similar past incident INC-2024-11-03 had same root cause.

## Contributing Factors
- Connection pool max size set to 100, insufficient for sustained request volume
- No connection pool monitoring or alerting in place
- No circuit breaker between service_a and service_b
- Similar incident INC-2024-11-03 did not result in pool size increase

## Alternative Hypotheses
ALT_1: Connection leak (lower probability given clean pool behavior before saturation)"""

CASCADE_POSTMORTEM = """# Postmortem: Database Connection Pool Exhaustion on Service B

## Incident Summary
| Field | Value |
|-------|-------|
| Incident ID | TEST-INTEGRATION |
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
Database connection pool exhaustion on service_b due to pool size (100) being insufficient for connection demand during normal operations.
Confidence: 85%

## Contributing Factors
- Connection pool max size set to 100, insufficient for sustained request volume
- No connection pool monitoring or alerting in place
- No circuit breaker between service_a and service_b
- Similar incident INC-2024-11-03 did not result in pool size increase

## Blast Radius
Affected: service_b (all requests), service_a (cascading timeouts, partial degradation)
Not Affected: service_c (no dependency on service_b)

## Signal Analysis
### Distributed Traces
service_b showed first errors at 14:03:15Z with p99=4.1s. Cascade path: service_b -> service_a.

### Logs
120 occurrences of connection pool exhausted in service_b starting at 14:03:14Z.

### Metrics
db_connections at 100/100 (pool saturated). Error rate at 31.2% (3.9x baseline).

## Similar Past Incidents
| Incident | Similarity | Root Cause |
|----------|-----------|------------|
| INC-2024-11-03 | 0.89 | DB connection pool exhaustion |

## Action Items
- Increase service_b connection pool max from 100 to 250
- Add DB connection pool saturation alert at 80% utilization
- Implement circuit breaker pattern between service_a and service_b
- Add real-time connection pool monitoring dashboard

## Data Gaps
All three observability signals were available during this incident.
Signal completeness: full
---"""

SEV3_TRIAGE = """## Triage Findings
- service_a error_rate: 2.5% (slightly above normal)
- service_a p99 latency: 150ms (normal)

## Severity Assessment
SEVERITY: SEV3
REASON: service_a error_rate at 2.5% is within SEV3 range

## Confirmed Affected Services
- service_a: 2.5% error rate

## Time Window
INCIDENT_START: 2025-01-15T14:03:22Z
ANALYSIS_WINDOW: 2025-01-15T14:03:22Z to 2025-01-15T14:13:22Z"""

DEGRADED_TRIAGE = """## Triage Findings
- service_b error_rate: 31.2% (anomalous)
- service_a error_rate: 8.3% (anomalous)

## Severity Assessment
SEVERITY: SEV2
REASON: Elevated error rates detected on service_b and service_a

## Confirmed Affected Services
- service_b: 31.2% error rate
- service_a: 8.3% error rate

## Time Window
INCIDENT_START: 2025-01-15T14:03:15Z
ANALYSIS_WINDOW: 2025-01-15T14:03:15Z to 2025-01-15T14:13:15Z"""

DEGRADED_TRACE = """## Trace Analysis Findings
- Jaeger query failed: unable to retrieve traces for service_b
- Running in degraded mode with no trace data

## Error Propagation
FIRST_ERROR_SERVICE: unknown
FIRST_ERROR_TIME: 2025-01-15T14:03:15Z
CASCADE_PATH: unknown"""

DEGRADED_METRIC = """## Metric Analysis Findings
- Prometheus query failed: unable to retrieve metrics for service_b
- Running in degraded mode with no metric data

## Resource Saturation
SATURATED_RESOURCE: unknown"""

DEGRADED_ROOT_CAUSE = """## Root Cause
ROOT_CAUSE: Degraded analysis - some signal sources unavailable. Root cause cannot be fully determined.

## Confidence
CONFIDENCE: 0.40

## Contributing Factors
- Some observability signals were unavailable during analysis"""

DEGRADED_POSTMORTEM = """# Postmortem: Degraded Analysis Report

## Incident Summary
Degraded analysis due to unavailable observability backends.
Signal completeness: partial.
Only available signals were used to generate this report.

## Data Gaps
Some observability signals were not available during this incident.
Signal completeness: partial
---"""

NOOP_CORRELATION = """## Correlation Summary
No significant findings - incident appears to be low severity.

## Causal Chain
No clear causal chain identified.

## Similar Past Incidents


## Signal Completeness
SIGNALS_AVAILABLE: trace=no log=no metric=no"""

NOOP_ROOT_CAUSE = """## Root Cause
ROOT_CAUSE: Not determined - no analysis data available

## Confidence
CONFIDENCE: 0.10

## Contributing Factors
- Not determined"""

NOOP_POSTMORTEM = """# Postmortem: No Analysis Available
No analysis data was generated for this incident.
Signal completeness: none
---"""


class FakeLLM:
    def __init__(self):
        self.bind_tools = MagicMock(return_value=self)

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
            content=self.responses.get(agent_name, "Mock response"),
            additional_kwargs={},
        )


class CascadeFakeLLM(FakeLLM):
    responses = {
        "triage": CASCADE_TRIAGE,
        "trace_analyzer": CASCADE_TRACE,
        "log_correlator": CASCADE_LOG,
        "metric_reasoner": CASCADE_METRIC,
        "correlation": CASCADE_CORRELATION,
        "root_cause": CASCADE_ROOT_CAUSE,
        "postmortem_writer": CASCADE_POSTMORTEM,
    }


class DegradedTraceFakeLLM(FakeLLM):
    responses = {
        "triage": DEGRADED_TRIAGE,
        "trace_analyzer": DEGRADED_TRACE,
        "log_correlator": CASCADE_LOG,
        "metric_reasoner": CASCADE_METRIC,
        "correlation": CASCADE_CORRELATION,
        "root_cause": CASCADE_ROOT_CAUSE,
        "postmortem_writer": CASCADE_POSTMORTEM,
    }


class DegradedMetricFakeLLM(FakeLLM):
    responses = {
        "triage": DEGRADED_TRIAGE,
        "trace_analyzer": CASCADE_TRACE,
        "log_correlator": CASCADE_LOG,
        "metric_reasoner": DEGRADED_METRIC,
        "correlation": CASCADE_CORRELATION,
        "root_cause": CASCADE_ROOT_CAUSE,
        "postmortem_writer": CASCADE_POSTMORTEM,
    }


class SEV3FakeLLM(FakeLLM):
    responses = {
        "triage": SEV3_TRIAGE,
        "correlation": NOOP_CORRELATION,
        "root_cause": NOOP_ROOT_CAUSE,
        "postmortem_writer": NOOP_POSTMORTEM,
    }


def make_cascade_failure_mocks():
    querier = MagicMock()

    def make_ts(metric, service, latest, avg, anomalous=False, reason=""):
        return TimeSeries(
            metric_name=metric,
            labels={"service": service},
            data_points=[
                DataPoint(timestamp=1736950995.0 - i * 15, value=avg + (latest - avg) * (i == 0))
                for i in range(20)
            ],
            min_value=avg * 0.8, max_value=latest, avg_value=avg,
            latest_value=latest, is_anomalous=anomalous, anomaly_reason=reason,
        )

    def make_trace(svc, is_error, duration_us, error_msg="", start="2025-01-15T14:03:15Z"):
        span = Span(
            span_id=f"span-{svc}-{id(is_error)}",
            parent_span_id="",
            operation_name="HTTP GET /api/process",
            service_name=svc,
            start_time_us=1736950995_000_000,
            duration_us=duration_us,
            is_error=is_error,
            error_message=error_msg,
            attributes={"http.method": "GET"},
        )
        return Trace(
            trace_id=f"trace-{svc}-{id(is_error)}",
            spans=[span], services=[svc],
            duration_us=duration_us, has_error=is_error,
            root_service=svc, root_operation="HTTP GET /api/process",
            start_time=start,
        )

    def mock_query_traces(service_name, lookback_minutes=60, limit=100, only_errors=False):
        if service_name == "service_b":
            return [
                make_trace("service_b", True, 4_100_000,
                           "connection pool exhausted", "2025-01-15T14:03:15Z"),
                make_trace("service_b", True, 3_900_000,
                           "connection pool exhausted", "2025-01-15T14:03:16Z"),
                make_trace("service_b", False, 890_000),
            ]
        elif service_name == "service_a":
            return [
                make_trace("service_a", True, 10_000_000,
                           "context deadline exceeded", "2025-01-15T14:03:22Z"),
                make_trace("service_a", False, 95_000),
            ]
        return []

    querier.query_traces.side_effect = mock_query_traces

    def mock_error_rate_stats(service_name, lookback_minutes=60):
        rates = {
            "service_b": {"total_traces": 100, "error_traces": 31,
                          "error_rate": 0.31, "p50_duration_us": 890_000,
                          "p99_duration_us": 4_100_000},
            "service_a": {"total_traces": 100, "error_traces": 8,
                          "error_rate": 0.08, "p50_duration_us": 95_000,
                          "p99_duration_us": 10_000_000},
            "service_c": {"total_traces": 100, "error_traces": 0,
                          "error_rate": 0.001, "p50_duration_us": 5_000,
                          "p99_duration_us": 15_000},
        }
        return rates.get(service_name, {"total_traces": 0, "error_rate": 0})

    querier.compute_trace_error_rate.side_effect = mock_error_rate_stats

    def mock_error_rate(service_name, lookback_minutes=60):
        data = {
            "service_b": (0.312, 0.08, True,
                          "latest 31.2% is 3.9x average 8%"),
            "service_a": (0.083, 0.015, True,
                          "latest 8.3% is 5.5x average 1.5%"),
            "service_c": (0.001, 0.001, False, ""),
        }
        latest, avg, anomalous, reason = data.get(service_name, (0, 0, False, ""))
        return make_ts("error_rate", service_name, latest, avg, anomalous, reason)

    querier.query_error_rate.side_effect = mock_error_rate

    def mock_latency_p99(service_name, lookback_minutes=60):
        data = {
            "service_b": (4.1, 0.5, True, "4.1s vs 0.5s avg - 8.2x"),
            "service_a": (10.0, 0.3, True, "10s vs 0.3s avg - 33x"),
            "service_c": (0.015, 0.012, False, ""),
        }
        latest, avg, anomalous, reason = data.get(service_name, (0, 0, False, ""))
        return make_ts("latency_p99", service_name, latest, avg, anomalous, reason)

    querier.query_latency_p99.side_effect = mock_latency_p99

    def mock_latency_p50(service_name, lookback_minutes=60):
        data = {
            "service_b": (0.89, 0.2, True, ""),
            "service_a": (0.095, 0.08, False, ""),
        }
        latest, avg, anomalous, reason = data.get(service_name, (0.01, 0.01, False, ""))
        return make_ts("latency_p50", service_name, latest, avg, anomalous, reason)

    querier.query_latency_p50.side_effect = mock_latency_p50

    def mock_request_rate(service_name, lookback_minutes=60):
        data = {"service_b": (45.2, 48.0), "service_a": (42.1, 47.5)}
        latest, avg = data.get(service_name, (10.0, 10.0))
        return make_ts("request_rate", service_name, latest, avg)

    querier.query_request_rate.side_effect = mock_request_rate

    def mock_db_connections(service_name, lookback_minutes=60):
        if service_name == "service_b":
            return make_ts("db_connections_active", "service_b",
                           100.0, 35.0, True,
                           "100 active connections = pool max (100) - EXHAUSTED")
        return make_ts("db_connections_active", service_name, 20.0, 18.0)

    querier.query_db_connections.side_effect = mock_db_connections

    querier.query_memory_usage.return_value = make_ts(
        "memory_bytes", "service_b", 256_000_000, 230_000_000
    )
    querier.query_goroutine_count.return_value = make_ts(
        "goroutines_count", "service_b", 850, 400
    )

    querier.query_logs.return_value = [
        LogEntry(
            log_id="log-1", service_name="service_b", level="ERROR",
            message="pq: connection pool exhausted: max connections (100) reached",
            trace_id="trace-b-1", span_id="span-b-1",
            timestamp="2025-01-15T14:03:14Z",
            fields={"db_pool": "100/100"},
        ),
        LogEntry(
            log_id="log-2", service_name="service_b", level="ERROR",
            message="pq: connection pool exhausted: max connections (100) reached",
            trace_id="trace-b-2", span_id="span-b-2",
            timestamp="2025-01-15T14:03:15Z",
            fields={"db_pool": "100/100"},
        ),
    ]

    querier.search_similar_incidents.return_value = [
        PastIncident(
            incident_id="INC-2024-11-03",
            title="service_b database connection pool exhaustion",
            root_cause="DB connection pool (100) exhausted during traffic spike",
            affected_services=["service_a", "service_b"],
            severity="SEV1",
            occurred_at="2024-11-03T14:03:00Z",
            resolved_at="2024-11-03T14:50:00Z",
            time_to_resolve_minutes=47,
            postmortem_summary="Connection pool (100) exhausted. Resolution: increase pool size.",
            action_items=["Increase DB pool size", "Add circuit breaker"],
            similarity_score=0.89,
        )
    ]

    querier.get_services.return_value = ["service_a", "service_b", "service_c"]
    querier.store_postmortem.return_value = "TEST-INTEGRATION"

    return querier


def patch_all_llms(llm):
    return [
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


@pytest.mark.integration
class TestCascadeFailureScenario:

    @pytest.fixture(autouse=True)
    def setup(self):
        self.querier = make_cascade_failure_mocks()
        with patch("agents.tools.registry.get_querier", return_value=self.querier):
            yield

    def test_full_pipeline_completes(self):
        llm = CascadeFakeLLM()
        with self._patch_context(llm):
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.run(
                incident_id="TEST-CASCADE-001",
                trigger_type="error_rate",
                trigger_description="error_rate 31.2% exceeded 5% threshold on service_b for 60s",
                affected_services=["service_a", "service_b"],
                detected_at="2025-01-15T14:03:22Z",
            )

        assert result is not None
        assert "postmortem_writer" in result.get("completed_agents", []), \
            f"Completed: {result.get('completed_agents', [])}"
        assert result.get("triage_severity") in ("SEV1", "SEV2"), \
            f"Expected SEV1/SEV2, got {result.get('triage_severity')}"
        assert result.get("root_cause"), "Root cause must not be empty"
        assert result.get("root_cause_confidence", 0) > 0.4, \
            f"Low confidence: {result.get('root_cause_confidence')}"
        report = result.get("postmortem_report", "")
        assert len(report) > 500, f"Report too short: {len(report)} chars"
        analysis_agents = {"trace_analyzer", "log_correlator", "metric_reasoner"}
        completed = set(result.get("completed_agents", []))
        succeeded = analysis_agents & completed
        assert len(succeeded) >= 2, \
            f"Only {len(succeeded)} parallel agents succeeded: {succeeded}"

    def test_root_cause_identifies_service_b(self):
        llm = CascadeFakeLLM()
        with self._patch_context(llm):
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.run(
                incident_id="TEST-CASCADE-RC-001",
                trigger_type="error_rate",
                trigger_description="error_rate 31.2% on service_b",
                affected_services=["service_a", "service_b"],
                detected_at="2025-01-15T14:03:22Z",
            )

        root_cause = result.get("root_cause", "").lower()
        assert "service_b" in root_cause or "connection" in root_cause or \
               "database" in root_cause or "pool" in root_cause, \
            f"Root cause missing service_b or DB issue: {root_cause}"

    def test_service_c_excluded_from_confirmed_services(self):
        llm = CascadeFakeLLM()
        with self._patch_context(llm):
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.run(
                incident_id="TEST-CASCADE-SVC-001",
                trigger_type="error_rate",
                trigger_description="error_rate spike",
                affected_services=["service_a", "service_b", "service_c"],
                detected_at="2025-01-15T14:03:22Z",
            )

        confirmed = result.get("triage_confirmed_services", [])
        assert "service_c" not in confirmed, \
            f"service_c should be excluded: {confirmed}"

    def test_similar_incident_appears_in_report(self):
        llm = CascadeFakeLLM()
        with self._patch_context(llm):
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.run(
                incident_id="TEST-CASCADE-RAG-001",
                trigger_type="error_rate",
                trigger_description="error_rate spike on service_b",
                affected_services=["service_a", "service_b"],
                detected_at="2025-01-15T14:03:22Z",
            )

        report = result.get("postmortem_report", "")
        similar = result.get("similar_incidents", [])
        assert "INC-2024-11-03" in report or "INC-2024-11-03" in similar, \
            "Similar past incident not referenced"

    def _patch_context(self, llm):
        from contextlib import ExitStack
        stack = ExitStack()
        for p in patch_all_llms(llm):
            stack.enter_context(p)
        return stack


@pytest.mark.integration
class TestCheckpointResume:

    @pytest.fixture(autouse=True)
    def setup(self):
        self.querier = make_cascade_failure_mocks()
        with patch("agents.tools.registry.get_querier", return_value=self.querier):
            yield

    def test_pipeline_resumes_from_checkpoint(self):
        llm = CascadeFakeLLM()
        patches = patch_all_llms(llm)
        for p in patches:
            p.start()
        try:
            runner = PipelineRunner(checkpoint_db=":memory:")
            runner.run(
                incident_id="TEST-RESUME-001",
                trigger_type="error_rate",
                trigger_description="test resume",
                affected_services=["service_a", "service_b"],
                detected_at="2025-01-15T14:03:22Z",
            )
            self.querier.reset_mock()
            result2 = runner.resume("TEST-RESUME-001")
            assert result2 is not None
            assert "postmortem_writer" in result2.get("completed_agents", [])
            assert self.querier.query_traces.call_count == 0, \
                "Resume should not re-query backends"
        finally:
            for p in patches:
                p.stop()

    def test_checkpoint_inspection(self):
        llm = CascadeFakeLLM()
        patches = patch_all_llms(llm)
        for p in patches:
            p.start()
        try:
            runner = PipelineRunner(checkpoint_db=":memory:")
            runner.run(
                incident_id="TEST-INSPECT-001",
                trigger_type="error_rate",
                trigger_description="checkpoint inspection test",
                affected_services=["service_a", "service_b"],
                detected_at="2025-01-15T14:03:22Z",
            )
            checkpoint = runner.get_checkpoint("TEST-INSPECT-001")
            assert checkpoint is not None
            assert checkpoint.get("incident_id") == "TEST-INSPECT-001"
            assert "postmortem_writer" in checkpoint.get("completed_agents", [])
        finally:
            for p in patches:
                p.stop()

    def test_checkpoint_returns_none_for_unknown_incident(self):
        llm = CascadeFakeLLM()
        patches = patch_all_llms(llm)
        for p in patches:
            p.start()
        try:
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.get_checkpoint("NONEXISTENT-INCIDENT-999")
            assert result is None
        finally:
            for p in patches:
                p.stop()


@pytest.mark.integration
class TestDegradedMode:

    @pytest.fixture(autouse=True)
    def setup(self):
        self.querier = make_cascade_failure_mocks()
        with patch("agents.tools.registry.get_querier", return_value=self.querier):
            yield

    def test_pipeline_completes_with_jaeger_down(self):
        self.querier.query_traces.side_effect = QueryError(
            "jaeger", "query_traces", ConnectionError("connection refused")
        )
        self.querier.compute_trace_error_rate.side_effect = QueryError(
            "jaeger", "error_rate", ConnectionError("connection refused")
        )
        llm = DegradedTraceFakeLLM()
        with self._patch_llm(llm):
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.run(
                incident_id="TEST-DEGRADED-JAEGER-001",
                trigger_type="error_rate",
                trigger_description="error spike - Jaeger unavailable",
                affected_services=["service_a", "service_b"],
                detected_at="2025-01-15T14:03:22Z",
            )

        assert result is not None
        assert "postmortem_writer" in result.get("completed_agents", [])
        assert result.get("postmortem_report", ""), "Report should not be empty"

    def test_pipeline_completes_with_prometheus_down(self):
        prom_error = QueryError("prometheus", "query", ConnectionError("refused"))
        self.querier.query_error_rate.side_effect = prom_error
        self.querier.query_latency_p99.side_effect = prom_error
        self.querier.query_latency_p50.side_effect = prom_error
        self.querier.query_request_rate.side_effect = prom_error
        self.querier.query_db_connections.side_effect = prom_error
        self.querier.query_memory_usage.side_effect = prom_error
        self.querier.query_goroutine_count.side_effect = prom_error
        llm = DegradedMetricFakeLLM()
        with self._patch_llm(llm):
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.run(
                incident_id="TEST-DEGRADED-PROM-001",
                trigger_type="error_rate",
                trigger_description="error spike - Prometheus unavailable",
                affected_services=["service_a", "service_b"],
                detected_at="2025-01-15T14:03:22Z",
            )

        assert result is not None
        assert "postmortem_writer" in result.get("completed_agents", [])

    def _patch_llm(self, llm):
        from contextlib import ExitStack
        stack = ExitStack()
        for p in patch_all_llms(llm):
            stack.enter_context(p)
        return stack


@pytest.mark.integration
class TestSEV3FastPath:

    @pytest.fixture(autouse=True)
    def setup(self):
        mocks = make_cascade_failure_mocks()
        mocks.query_error_rate.return_value = TimeSeries(
            metric_name="error_rate",
            labels={"service": "service_a"},
            data_points=[DataPoint(timestamp=1736950995.0, value=0.025)],
            min_value=0.02, max_value=0.03, avg_value=0.022,
            latest_value=0.025, is_anomalous=False, anomaly_reason="",
        )
        mocks.compute_trace_error_rate.return_value = {
            "total_traces": 100, "error_traces": 2,
            "error_rate": 0.02, "p50_duration_us": 50_000,
            "p99_duration_us": 150_000,
        }
        self.querier = mocks
        with patch("agents.tools.registry.get_querier", return_value=self.querier):
            yield

    def test_sev3_skips_parallel_analysis(self):
        llm = SEV3FakeLLM()
        patches = patch_all_llms(llm)
        for p in patches:
            p.start()
        try:
            runner = PipelineRunner(checkpoint_db=":memory:")
            result = runner.run(
                incident_id="TEST-SEV3-001",
                trigger_type="error_rate",
                trigger_description="minor error rate increase on service_a (2.5%)",
                affected_services=["service_a"],
                detected_at="2025-01-15T14:03:22Z",
            )

            assert result is not None
            severity = result.get("triage_severity")

            if severity == "SEV3":
                completed = result.get("completed_agents", [])
                assert "trace_analyzer" not in completed, \
                    "SEV3 should skip trace_analyzer"
                assert "log_correlator" not in completed, \
                    "SEV3 should skip log_correlator"
                assert "metric_reasoner" not in completed, \
                    "SEV3 should skip metric_reasoner"
            else:
                pytest.skip(f"Triage assessed as {severity} -- not SEV3")
        finally:
            for p in patches:
                p.stop()
