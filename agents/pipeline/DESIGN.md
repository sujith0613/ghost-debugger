# Ghost Debugger — Agent Pipeline Design

**Version:** 1.0
**Status:** Design Complete — Ready for Implementation
**Last Updated:** 28-06-2026

---

## 1. Pipeline Overview

The agent pipeline is a LangGraph StateGraph that transforms an
IncidentNotification (from the gateway's incident detector) into a
structured postmortem report.

### Execution Model

```
INCIDENT DETECTED
       │
       ▼
┌─────────────┐
│   TRIAGE    │  Sequential — determines scope
│   AGENT     │  Duration: ~15s (2-3 LLM calls + tool calls)
└──────┬──────┘
       │
       ▼
   ┌───┴───┐
   │ Route │  Conditional edge:
   │       │  - severity SEV1/SEV2 → full parallel analysis
   │       │  - severity SEV3 → fast path (skip parallel, go to correlation)
   └───┬───┘
       │
       ▼ (SEV1/SEV2)
┌─────────────────────────────────────────────┐
│          PARALLEL ANALYSIS (fan-out)         │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │  TRACE   │  │   LOG    │  │  METRIC  │   │
│  │  AGENT   │  │  AGENT   │  │  AGENT   │   │
│  │          │  │          │  │          │   │
│  │ ~20s     │  │ ~15s     │  │ ~20s     │   │
│  │ 2-4 tool │  │ 2-3 tool │  │ 3-5 tool │   │
│  │ calls    │  │ calls    │  │ calls    │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
│       │              │              │         │
└───────┴──────────────┴──────────────┴─────────┘
       │
       ▼ (fan-in: wait for ALL three to complete)
┌─────────────────┐
│  CORRELATION    │  Sequential — synthesizes all findings
│  AGENT          │  Duration: ~15s (1-2 LLM calls + RAG)
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│  ROOT CAUSE     │  Sequential — determines definitive cause
│  AGENT          │  Duration: ~10s (1 LLM call, pure reasoning)
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│  POSTMORTEM     │  Sequential — generates final report
│  WRITER         │  Duration: ~10s (1 LLM call, generation)
└──────┬──────────┘
       │
       ▼
    COMPLETE
```

### Total Pipeline Duration Target

| Phase | Duration | Notes |
|-------|----------|-------|
| Triage | ~15s | 2-3 LLM calls + Jaeger/Prometheus queries |
| Parallel Analysis | ~20s | Wall-clock = slowest agent (not sum) |
| Correlation | ~15s | 1-2 LLM calls + ChromaDB RAG |
| Root Cause | ~10s | 1 LLM call, pure reasoning |
| Postmortem Writer | ~10s | 1 LLM call, generation |
| **Total** | **~70s** | **Within 60-90s target** |

---

## 2. State Schema

The `PostmortemState` TypedDict is the single source of truth.
Every agent reads from and writes to this state.
No agent has private state — everything is in the shared TypedDict.

### Field Ownership

Each field is owned by exactly one agent. Only the owning agent writes to it.
Any agent can read any field.

| Field | Owner | Writers | Readers |
|-------|-------|---------|---------|
| incident_id | Pipeline | pipeline entry | all |
| trigger_type | Pipeline | pipeline entry | all |
| trigger_description | Pipeline | pipeline entry | all |
| affected_services | Pipeline | pipeline entry | all |
| detected_at | Pipeline | pipeline entry | all |
| analysis_window_seconds | Pipeline | pipeline entry | all |
| triage_findings | Triage | triage | correlation, root_cause |
| triage_severity | Triage | triage | router, all |
| triage_time_window | Triage | triage | trace, log, metric |
| triage_confirmed_services | Triage | triage | trace, log, metric |
| trace_findings | Trace | trace | correlation |
| trace_first_error_service | Trace | trace | correlation, root_cause |
| trace_first_error_time | Trace | trace | correlation |
| trace_cascade_path | Trace | trace | correlation |
| trace_had_error | Trace | trace | correlation (for degraded mode) |
| log_findings | Log | log | correlation |
| log_error_patterns | Log | log | correlation |
| log_first_error_time | Log | log | correlation |
| log_had_error | Log | log | correlation (for degraded mode) |
| metric_findings | Metric | metric | correlation |
| metric_saturated_resource | Metric | metric | correlation, root_cause |
| metric_anomaly_details | Metric | metric | correlation |
| metric_had_error | Metric | metric | correlation (for degraded mode) |
| correlation_summary | Correlation | correlation | root_cause, postmortem |
| causal_chain | Correlation | correlation | root_cause, postmortem |
| similar_incidents | Correlation | correlation | root_cause, postmortem |
| root_cause | Root Cause | root_cause | postmortem |
| root_cause_confidence | Root Cause | root_cause | postmortem |
| contributing_factors | Root Cause | root_cause | postmortem |
| postmortem_report | Writer | postmortem | pipeline output |
| signal_completeness | Writer | postmortem | pipeline output |
| completed_agents | Pipeline | all agents | pipeline output |
| failed_agents | Pipeline | all agents | pipeline output |
| errors | Pipeline | all agents | pipeline output |

### State Initialization

All fields are initialized to safe defaults (empty lists, empty strings, 0.0).
No field is ever `None` or uninitialized. This prevents KeyError in agents
when a previous agent failed to populate a field.

---

## 3. Node Contracts

### 3.1 Triage Agent

**Purpose:** Determine the scope and severity of the incident.

**Reads from state:**
- `incident_id` — which incident
- `trigger_type` — what triggered detection
- `trigger_description` — human-readable trigger
- `affected_services` — initial suspected services from gateway
- `detected_at` — when the incident was detected
- `analysis_window_seconds` — how far back to look

**Writes to state:**
- `triage_findings: List[str]` — natural language findings
- `triage_severity: str` — "SEV1" | "SEV2" | "SEV3"
- `triage_time_window: str` — refined time window (ISO 8601 range)
- `triage_confirmed_services: List[str]` — services confirmed as affected
- `completed_agents` — appends "triage"
- `errors` — appends error if any tool call fails

**Tools available:**
- `query_traces(service_name, lookback_minutes)` — check trace error rates
- `query_error_rate(service_name, lookback_minutes)` — check Prometheus error rates

**Behavior:**
1. For each service in `affected_services`:
   a. Query trace error rate from Jaeger
   b. Query HTTP error rate from Prometheus
2. Compare current rates against baseline (pre-incident window)
3. Determine severity:
   - SEV1: error_rate > 20% OR multiple services affected
   - SEV2: error_rate > 5% on one service
   - SEV3: error_rate > 2% OR latency spike only
4. Refine the time window based on when anomalies first appeared
5. Confirm which services are actually affected (gateway may over-report)

**Failure handling:**
- If Jaeger is down: use Prometheus error_rate only, note in errors
- If Prometheus is down: use Jaeger trace error_rate only, note in errors
- If both are down: set severity to "UNKNOWN", note in errors, continue

---

### 3.2 Trace Analysis Agent

**Purpose:** Analyze distributed traces to find the failure origin and cascade path.

**Reads from state:**
- `triage_confirmed_services` — which services to investigate
- `triage_time_window` — refined time window from triage
- `detected_at` — reference point

**Writes to state:**
- `trace_findings: List[str]` — natural language trace analysis
- `trace_first_error_service: str` — which service errored first
- `trace_first_error_time: str` — when the first error appeared
- `trace_cascade_path: List[str]` — ordered list of services in failure cascade
- `trace_had_error: bool` — True if analysis completed successfully
- `completed_agents` — appends "trace_analyzer"
- `errors` — appends error if Jaeger is unavailable

**Tools available:**
- `query_traces(service_name, lookback_minutes, only_errors=True)` — get error traces
- `get_trace(trace_id)` — get full trace tree for a specific trace
- `compute_trace_error_rate(service_name, lookback_minutes)` — error rate stats

**Behavior:**
1. For each confirmed service, query error traces in the time window
2. For the top 5 error traces, fetch full trace trees
3. Analyze the trace tree to determine:
   a. Which span errored first (temporal ordering)
   b. Which service that span belongs to
   c. How the error propagated (parent → child or child → parent)
4. Construct the cascade path: [first_service, second_service, ...]
5. Identify the specific error types (timeout, connection refused, OOM, etc.)

**Failure handling:**
- If Jaeger is down: set `trace_had_error = False`, write error to `errors`,
  set findings to ["Trace analysis unavailable — Jaeger unreachable"]
- Do NOT crash the pipeline. The correlation agent handles missing trace data.

---

### 3.3 Log Analysis Agent

**Purpose:** Analyze error logs to find error patterns and first occurrences.

**Reads from state:**
- `triage_confirmed_services`
- `triage_time_window`
- `detected_at`

**Writes to state:**
- `log_findings: List[str]`
- `log_error_patterns: List[str]` — clustered error message patterns
- `log_first_error_time: str`
- `log_had_error: bool`
- `completed_agents` — appends "log_correlator"
- `errors`

**Tools available:**
- `query_logs(service_name, level="ERROR", lookback_minutes)` — get error logs
- `query_logs(service_name, level="WARN", lookback_minutes)` — get warning logs

**Behavior:**
1. Query ERROR logs for each confirmed service
2. Cluster similar error messages (group by error type)
3. Identify the first occurrence of each error pattern
4. Cross-reference log timestamps with trace timestamps
5. Identify error messages that indicate specific failure modes:
   - "connection refused" → downstream service down
   - "connection pool exhausted" → resource saturation
   - "deadline exceeded" → timeout cascade
   - "out of memory" → memory exhaustion
   - "too many open files" → file descriptor exhaustion

**Failure handling:**
- If log storage is unavailable: set `log_had_error = False`, continue
- Logs are the least critical signal — trace + metric is usually sufficient

---

### 3.4 Metric Analysis Agent

**Purpose:** Analyze Prometheus metrics to identify resource saturation and anomalies.

**Reads from state:**
- `triage_confirmed_services`
- `triage_time_window`
- `detected_at`

**Writes to state:**
- `metric_findings: List[str]`
- `metric_saturated_resource: str` — "db_connections" | "cpu" | "memory" | "goroutines" | ""
- `metric_anomaly_details: List[str]` — specific metric anomalies found
- `metric_had_error: bool`
- `completed_agents` — appends "metric_reasoner"
- `errors`

**Tools available:**
- `query_error_rate(service_name, lookback_minutes)` — Prometheus error rate
- `query_latency_p99(service_name, lookback_minutes)` — p99 latency
- `query_latency_p50(service_name, lookback_minutes)` — median latency
- `query_request_rate(service_name, lookback_minutes)` — requests/second
- `query_db_connections(service_name, lookback_minutes)` — DB pool usage
- `query_memory_usage(service_name, lookback_minutes)` — memory bytes
- `query_goroutine_count(service_name, lookback_minutes)` — goroutine count

**Behavior:**
1. For each confirmed service, query all available metrics
2. For each metric, check if `is_anomalous` is True (computed by PrometheusClient)
3. Identify the specific resource that saturated:
   - db_connections_active == db_connections_max → DB pool exhaustion
   - memory_bytes approaching container limit → memory pressure
   - goroutine_count > 10000 → goroutine leak
   - p99_latency > 5x p50_latency → latency spike
4. Determine the timestamp when the anomaly first appeared
5. Correlate metric anomalies with the incident trigger time

**Failure handling:**
- If Prometheus is down: set `metric_had_error = False`, continue
- If specific metrics are missing (e.g., no db_connections metric): skip, note in findings

---

### 3.5 Correlation Agent

**Purpose:** Synthesize findings from all three analysis agents into a causal chain.

**Reads from state:**
- `triage_findings`, `triage_severity`, `triage_confirmed_services`
- `trace_findings`, `trace_first_error_service`, `trace_first_error_time`, `trace_cascade_path`, `trace_had_error`
- `log_findings`, `log_error_patterns`, `log_first_error_time`, `log_had_error`
- `metric_findings`, `metric_saturated_resource`, `metric_anomaly_details`, `metric_had_error`

**Writes to state:**
- `correlation_summary: str` — narrative explaining how findings relate
- `causal_chain: List[str]` — ordered sequence of events
- `similar_incidents: List[str]` — incident IDs from RAG search
- `completed_agents` — appends "correlation"
- `errors`

**Tools available:**
- `search_similar_incidents(query_text, n_results=3)` — RAG over past postmortems

**Behavior:**
1. Check which signals are available (trace_had_error, log_had_error, metric_had_error)
2. Build a temporal timeline from all available signals:
   - Sort events by timestamp across all three signal types
   - Identify the first anomaly across all signals
3. Determine the causal chain:
   - "At 14:03:12, service_b DB connections hit 100/100 (metric)"
   - "At 14:03:15, service_b began returning errors (trace)"
   - "At 14:03:22, service_a began timing out on service_b calls (trace)"
   - "At 14:03:25, ERROR logs show 'connection pool exhausted' (log)"
4. Query ChromaDB for similar past incidents using the causal chain as query
5. Note any matches: "This pattern matches INC-2024-11-03 (similarity: 0.89)"

**Degraded mode handling:**
- If only 2 of 3 signals available: proceed with available data, note gap
- If only 1 signal available: proceed but set low confidence
- If 0 signals available: this should not happen (triage would have caught it),
  but handle gracefully: "Insufficient data for correlation"

---

### 3.6 Root Cause Agent

**Purpose:** Determine the definitive root cause from the correlation summary.

**Reads from state:**
- `correlation_summary`
- `causal_chain`
- `similar_incidents`
- `triage_severity`
- `metric_saturated_resource`
- `trace_first_error_service`

**Writes to state:**
- `root_cause: str` — concise root cause statement
- `root_cause_confidence: float` — 0.0 to 1.0
- `contributing_factors: List[str]` — conditions that enabled the failure
- `completed_agents` — appends "root_cause"

**Tools available:** None — pure reasoning node.

**Behavior:**
1. Read the causal chain and correlation summary
2. Identify the FIRST event in the causal chain — this is the root cause
3. Distinguish root cause from symptoms
4. If similar incidents exist, cross-reference their root causes
5. Assign confidence based on signal agreement
6. List contributing factors (conditions that made the root cause possible)

---

### 3.7 Postmortem Writer Agent

**Purpose:** Generate the final structured postmortem report in markdown.

**Reads from state:** ALL fields.

**Writes to state:**
- `postmortem_report: str` — complete markdown document
- `signal_completeness: str` — "full" | "partial"
- `completed_agents` — appends "postmortem_writer"

**Tools available:** None — pure generation node.

**Behavior:**
1. Check signal completeness:
   - "full" if trace_had_error AND log_had_error AND metric_had_error
   - "partial" otherwise
2. Generate structured markdown report with sections:
   - Incident Summary (ID, severity, duration, affected services)
   - Timeline (from causal_chain, with timestamps)
   - Root Cause (from root_cause field, with confidence)
   - Contributing Factors
   - Blast Radius (what was affected, what was NOT affected)
   - Signal Analysis (trace findings, log findings, metric findings)
   - Similar Past Incidents (from RAG results)
   - Action Items (immediate, short-term, long-term)
   - Data Gaps (if signal_completeness is "partial")
3. If partial: add explicit disclaimer about missing signals

---

## 4. Edge Definitions

### 4.1 Sequential Edges

```
START → triage
triage → route_decision (conditional)
correlation → root_cause
root_cause → postmortem_writer
postmortem_writer → END
```

### 4.2 Conditional Edge: route_decision

After the triage agent completes, the pipeline checks severity:

```python
def route_after_triage(state: PostmortemState) -> str:
    severity = state.get("triage_severity", "UNKNOWN")
    if not state.get("triage_confirmed_services"):
        return "postmortem_writer"
    if severity == "SEV3":
        return "correlation"
    return "parallel_analysis"
```

### 4.3 Parallel Fan-Out/Fan-In

LangGraph handles parallelism via the `Send` API:

```python
from langgraph.types import Send

def fan_out_parallel_agents(state: PostmortemState):
    return [
        Send("trace_analyzer", state),
        Send("log_correlator", state),
        Send("metric_reasoner", state),
    ]
```

### 4.4 Complete Edge Map

```
START
  → triage

triage
  → route_decision (conditional)
      → "parallel_analysis" (SEV1/SEV2)
      → "correlation" (SEV3 fast path)
      → "postmortem_writer" (no services affected)

parallel_analysis
  → [Send("trace_analyzer"), Send("log_correlator"), Send("metric_reasoner")]
  → (fan-in: wait for all three)
  → correlation

correlation
  → root_cause

root_cause
  → postmortem_writer

postmortem_writer
  → END
```

---

## 5. Error Propagation Model

1. Each agent catches its own exceptions internally
2. It writes the error to `state["errors"]`
3. It sets its `*_had_error` flag to False
4. It writes a finding like "Analysis unavailable — [reason]"
5. The pipeline continues to the next agent

### LLM API failure handling

1. Each agent uses exponential backoff (tenacity library)
2. 3 retries with 1s, 2s, 4s delays
3. After 3 failures: agent records error, sets had_error=False, continues

### Storage backend failure handling

1. The TelemetryQuerier raises QueryError
2. The agent catches QueryError
3. The agent records which backend is down in errors
4. The agent writes findings noting the data gap
5. The correlation agent sees the gap and adjusts confidence

### The pipeline NEVER crashes mid-execution.

Worst case: all agents fail, postmortem_writer generates a report
saying "Insufficient data — all analysis agents encountered errors".

---

## 6. Checkpointing Strategy

LangGraph checkpointing saves state after EVERY node completion.

**Checkpointer:** SQLite (single instance) or PostgreSQL (multi-instance)
**Thread ID:** incident_id (one thread per incident)

**Recovery scenario:**
- Pipeline crashes after trace_analyzer completes
- LangGraph restarts with same incident_id
- State is loaded from checkpoint (triage + trace findings present)
- Pipeline resumes from log_correlator (not from triage)
- No duplicate LLM calls for completed agents

**Checkpoint data size:** ~10-50KB per incident
**Retention:** Keep checkpoints for 7 days, then clean up
