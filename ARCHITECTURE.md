# Ghost Debugger — Architecture Document

**Version:** 1.0
**Author:** Sujith M
**Status:** Living Document — updated as design evolves
**Last Updated:** 27-06-2026

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [System Components](#2-system-components)
3. [Component Interfaces](#3-component-interfaces)
4. [Failure Modes & Resilience](#4-failure-modes--resilience)
5. [Performance Targets](#5-performance-targets)
6. [Design Tradeoffs](#6-design-tradeoffs)
7. [Agent Pipeline Architecture](#8-agent-pipeline-architecture)
8. [Future Work](#9-future-work)

---

## 1. Problem Statement

### Who is the user?

The primary user is a **Site Reliability Engineer (SRE)** or **platform engineer** who is on-call and responding to a production incident.

Secondary users are engineering managers and post-incident reviewers who need structured postmortem documentation after an incident is resolved.

### What pain point exists?

When a distributed system fails, signals fragment across three isolated observability tools simultaneously:

- **Traces** in Jaeger show the request flow — but require manual span-by-span inspection to find where latency spiked or errors propagated
- **Metrics** in Prometheus show resource behavior — but require writing PromQL queries under pressure, often without knowing which service to query first
- **Logs** in stdout/files show error messages — but are unstructured, high-volume, and spread across multiple services

An SRE responding to a PagerDuty alert at 3am must:

1. Open Jaeger, find the relevant trace ID (if they even have one)
2. Open Grafana, write PromQL queries for each potentially affected service
3. SSH into multiple machines or open log aggregators to search logs
4. Mentally correlate events across all three sources by timestamp
5. Determine which service failed first and why
6. Write a postmortem document from scratch

**This process takes 45 minutes to 4 hours depending on incident complexity.**

The core problem is not that the data does not exist. The data is there. The problem is that no single tool correlates signals across all three observability pillars automatically and reasons about causality — it requires a human to hold the mental model together under pressure.

### What Ghost Debugger does

Ghost Debugger is a distributed incident analysis system that:

1. Continuously ingests OpenTelemetry traces, Prometheus metrics, and structured logs from instrumented microservices via a Go gRPC gateway
2. On incident detection (metric threshold breach or error rate spike), triggers a LangGraph multi-agent pipeline that analyzes each signal type in parallel using specialized agents
3. Correlates findings across agents using temporal reasoning and RAG-powered retrieval over historical postmortems
4. Generates a structured postmortem report — root cause, timeline, blast radius, action items — within 60 seconds of incident detection

The target reduction: from ~2 hours of manual correlation to under 60 seconds of automated analysis, with a human reviewing and approving the output.

---

## 2. System Components

The system has six distinct layers, each with a clearly bounded responsibility.

---

### 2.1 Test Microservices (Signal Generators)

**Components:** `service_a`, `service_b`, `service_c`, `failure_injector`

**Responsibility:**
Simulate a realistic distributed system that generates real observability signals. These are not mock services — they make actual HTTP calls to each other, propagate OpenTelemetry trace context across service boundaries, emit real Prometheus metrics, and produce structured JSON logs.

**Call topology:**

```
User Request → service_a → service_b → service_c
(each hop propagates W3C TraceContext headers)
```

**What each service does:**
- Exposes an HTTP endpoint that simulates work (configurable latency, random database calls, downstream service calls)
- Instruments every incoming request with an OpenTelemetry span
- Propagates trace context to downstream services via HTTP headers
- Emits Prometheus metrics: request_count, request_duration_seconds, error_count, memory_usage_bytes
- Logs structured JSON: timestamp, level, trace_id, span_id, service, message

**failure_injector:**
A separate control service that can be instructed to:
- Inject artificial latency into any service (simulates slow database)
- Force error responses at a configurable rate (simulates upstream failure)
- Exhaust goroutine pool (simulates resource saturation)
- Trigger OOM condition (simulates memory leak)

This component is the "chaos" layer. It creates the incidents that Ghost Debugger analyzes.

---

### 2.2 Go Telemetry Gateway

**Language:** Go
**Protocol:** gRPC (inbound from services, outbound to agents)

**Responsibility:**
The central nervous system of the ingestion layer. Accepts telemetry from instrumented services, applies rate limiting and circuit breaking, routes signals to storage backends, and invokes the agent pipeline when an incident is detected.

**Sub-components:**

| Sub-component | Responsibility |
|---------------|----------------|
| gRPC Server | Accepts TraceIngestionRequest, LogIngestionRequest, MetricIngestionRequest |
| Rate Limiter | Token bucket per service — prevents telemetry storms from overwhelming the system |
| Incident Detector | Watches metric streams for threshold breaches and error rate spikes |
| Circuit Breaker | 3-state machine (Closed/Open/Half-Open) protecting agent invocation |
| Agent Router | Routes incident analysis requests to the Python agent service via gRPC |
| Metrics Exporter | Exposes /metrics endpoint for Prometheus to scrape gateway health |

**What it does NOT do:**
The gateway does not analyze telemetry. It does not make any intelligence decisions. It is a reliable, high-throughput ingestion and routing layer. Intelligence lives entirely in the agent layer.

---

### 2.3 Telemetry Storage Layer

**Components:** Jaeger, Prometheus, ChromaDB

**Responsibility:**
Persist telemetry signals in queryable form. Each storage backend is specialized for its signal type.

| Backend | Signal Type | Query Interface |
|---------|-------------|-----------------|
| Jaeger | Distributed traces (spans, timing, errors) | Jaeger gRPC API / HTTP API |
| Prometheus | Time-series metrics (rates, gauges, histograms) | PromQL via HTTP API |
| ChromaDB | Past postmortem reports (vector embeddings) | Semantic similarity search |

**ChromaDB specifically:**
Stores embeddings of every postmortem report generated by the system. When a new incident is analyzed, the Correlation Agent queries ChromaDB with an embedding of the current incident's characteristics to retrieve the most semantically similar past incidents. This is the RAG layer — it gives agents institutional memory.

---

### 2.4 Python Agent Service

**Language:** Python
**Framework:** LangGraph (StateGraph)
**LLM:** Google Gemini API
**Interface:** gRPC server (receives AnalysisRequest, returns AnalysisResponse)

**Responsibility:**
Execute the multi-agent reasoning pipeline. This is where all intelligence lives. The agent service receives an incident ID from the Go gateway, queries storage backends for relevant telemetry, runs the LangGraph pipeline, and returns a completed postmortem report.

**Agent pipeline (sequential with internal parallelism):**

```
TRIAGE AGENT
↓
┌─────────────────────────────┐
│ TRACE AGENT │ LOG AGENT │ METRIC AGENT │ (parallel)
└─────────────────────────────┘
↓
CORRELATION AGENT (+ RAG)
↓
ROOT CAUSE AGENT
↓
POSTMORTEM WRITER
```

Each agent is a LangGraph node. Each node receives the shared PostmortemState, adds its findings, and passes the enriched state to the next node.

**Tools available to agents:**
Each agent has access to a set of typed Python functions decorated as LangChain tools. These functions call Jaeger, Prometheus, and ChromaDB query interfaces. Agents decide which tools to call, with which parameters, based on the current state and LLM reasoning.

---

### 2.5 FastAPI Endpoint

**Language:** Python
**Framework:** FastAPI

**Responsibility:**
HTTP interface for triggering manual analysis and retrieving postmortem reports. Wraps the gRPC agent service in an HTTP API for the web UI.

**Endpoints:**

```
POST /analyze         — trigger analysis for a given incident_id
GET  /incidents       — list all analyzed incidents
GET  /incidents/{id}  — retrieve postmortem report for incident
GET  /health          — service health check
```

---

### 2.6 Web Dashboard

**Language:** React (minimal) or plain HTML + JavaScript
**Responsibility:**
Human-readable interface for viewing postmortem reports and observing agent reasoning in real time.

**Key design principle:**
The dashboard shows the agent reasoning chain, not just the final output. Each agent step is rendered as it completes — "Triage Agent: identified 3 affected services (2.3s)" — because transparency in AI reasoning is an engineering requirement, not a UX nicety. Operators need to know *how* the system reached its conclusion to trust and override it.

---

## 3. Component Interfaces

### 3.1 System Architecture Diagram

```
                    ┌─────────────────────────────────────────────────────┐
                    │                  TEST SERVICES                      │
                    │                                                     │
                    │  [service_a] ──HTTP+OTel──► [service_b] ──► [service_c]  │
                    │       │              │              │                │
                    │  [failure_injector] (chaos control plane)           │
                    └────────┬────────────┬─────────────┬────────────────┘
                             │            │             │
                    OTel gRPC│       Prom scrape   JSON logs
                             │            │             │
                    ┌────────▼────────────▼─────────────▼────────────────┐
                    │              GO TELEMETRY GATEWAY                   │
                    │                                                     │
                    │  gRPC Server → Rate Limiter → Incident Detector     │
                    │                                    │                │
                    │                          Circuit Breaker            │
                    │                                    │                │
                    │                          Agent Router (gRPC out)   │
                    │                                    │                │
                    │  /metrics endpoint ◄── Metrics Exporter            │
                    └────────┬──────────────┬────────────┬───────────────┘
                             │              │            │
                    gRPC     │       gRPC   │    HTTP    │
                    (traces) │    (metrics) │   (logs)   │
                             │              │            │
                    ┌────────▼──┐  ┌────────▼──┐  ┌─────▼──────┐
                    │  JAEGER   │  │PROMETHEUS │  │  ChromaDB  │
                    │ (traces)  │  │ (metrics) │  │   (RAG)    │
                    └────────┬──┘  └────────┬──┘  └─────┬──────┘
                             │              │            │
                             └──────────────┴────────────┘
                                            │
                                  queried by agents
                                            │
                    ┌───────────────────────▼─────────────────────────────┐
                    │           PYTHON AGENT SERVICE (LangGraph)          │
                    │                                                     │
                    │  Triage → [Trace‖Log‖Metric] → Correlation →       │
                    │           Root Cause → Postmortem Writer            │
                    │                                                     │
                    │  gRPC server (receives AnalysisRequest)             │
                    └───────────────────────┬─────────────────────────────┘
                                            │
                                       FastAPI
                                            │
                    ┌───────────────────────▼─────────────────────────────┐
                    │                  WEB DASHBOARD                      │
                    │  (agent reasoning chain + postmortem report)        │
                    └─────────────────────────────────────────────────────┘
```

---

### 3.2 gRPC Interface Contracts

#### telemetry.proto

```protobuf
syntax = "proto3";
package telemetry;

message Span {
  string span_id        = 1;
  string parent_span_id = 2;
  string operation_name = 3;
  string service_name   = 4;
  int64  start_time_ns  = 5;
  int64  duration_ns    = 6;
  bool   is_error       = 7;
  map<string, string> tags = 8;
}

message TraceIngestionRequest {
  string trace_id         = 1;
  repeated Span spans     = 2;
  string source_service   = 3;
  int64  ingested_at_ns   = 4;
}

message LogIngestionRequest {
  string log_id         = 1;
  string service_name   = 2;
  string level          = 3;
  string message        = 4;
  string trace_id       = 5;
  string span_id        = 6;
  int64  timestamp_ns   = 7;
  map<string, string> fields = 8;
}

message MetricDataPoint {
  string metric_name         = 1;
  double value               = 2;
  map<string, string> labels = 3;
  int64  timestamp_ns        = 4;
}

message MetricIngestionRequest {
  string source_service             = 1;
  repeated MetricDataPoint points   = 2;
}

message IngestionResponse {
  string status       = 1;
  string ingestion_id = 2;
  string message      = 3;
}

service TelemetryService {
  rpc IngestTrace  (TraceIngestionRequest)  returns (IngestionResponse);
  rpc IngestLog    (LogIngestionRequest)    returns (IngestionResponse);
  rpc IngestMetric (MetricIngestionRequest) returns (IngestionResponse);
}
```

#### agent.proto

```protobuf
syntax = "proto3";
package agent;

message AnalysisRequest {
  string incident_id          = 1;
  string detected_at_ns       = 2;
  repeated string services    = 3;
  string trigger_type         = 4;
  string trigger_description  = 5;
  int64  analysis_window_ns   = 6;
}

message AgentFinding {
  string agent_name     = 1;
  string finding        = 2;
  double confidence     = 3;
  int64  completed_at   = 4;
}

message AnalysisResponse {
  string incident_id                  = 1;
  repeated AgentFinding findings      = 2;
  string root_cause                   = 3;
  double root_cause_confidence        = 4;
  string postmortem_markdown          = 5;
  repeated string similar_incidents   = 6;
  int64  analysis_duration_ms         = 7;
  string status                       = 8;
}

service AgentService {
  rpc AnalyzeIncident (AnalysisRequest) returns (AnalysisResponse);
}
```

---

### 3.3 OpenTelemetry Context Propagation

Test services propagate trace context using the **W3C TraceContext** standard via HTTP headers:

```
traceparent: 00-{trace_id}-{span_id}-{flags}
```

Every outgoing HTTP call from service_a to service_b includes this header. service_b extracts the context, creates a child span under the same trace, and propagates forward to service_c.

This means a single user request generates one trace with spans from all three services — correctly parented. Jaeger displays this as a single unified trace tree.

The Go gateway receives traces via the **OpenTelemetry gRPC OTLP exporter** (port 4317). Services are configured with the OTel SDK pointing to the gateway's OTLP endpoint.

---

### 3.4 Prometheus Scrape Configuration

The Go gateway exposes a `/metrics` endpoint on port `9090`. Each test service exposes `/metrics` on its own port. Prometheus is configured to scrape all of them at 15-second intervals.

```yaml
scrape_configs:
  - job_name: 'gateway'
    static_configs:
      - targets: ['gateway:9090']
  - job_name: 'test_services'
    static_configs:
      - targets: ['service_a:8081', 'service_b:8082', 'service_c:8083']
```

---

## 4. Failure Modes & Resilience

---

### 4.1 Agent Service is Unavailable

**Scenario:** The Python agent service crashes or is unreachable when the Go gateway tries to invoke incident analysis.

**Detection:** The circuit breaker in the Go gateway monitors the gRPC call success rate to the agent service.

**Behavior:**

```
State: CLOSED (normal operation)
  → Agent call fails N times within M seconds
  → Circuit transitions to OPEN

State: OPEN
  → All agent invocation calls are immediately rejected (fail fast)
  → Gateway logs: "circuit open — agent service unavailable"
  → Incident is queued with status "pending_analysis"
  → After timeout T, circuit transitions to HALF-OPEN

State: HALF-OPEN
  → One probe request is sent to the agent service
  → If it succeeds: circuit closes, queued incidents are processed
  → If it fails: circuit returns to OPEN
```

**Design decision:** Fail fast is preferred over retry-with-backoff at the gateway level because the agent service performs LLM calls (Gemini API) that can take 10–30 seconds. Stacking retries would exhaust goroutine pools.

---

### 4.2 Individual Agent Node Fails Mid-Pipeline

**Scenario:** The LangGraph pipeline is running. The Trace Agent completes. The Log Agent raises an unhandled exception.

**Behavior:**
LangGraph is configured with SQLite checkpointing. After each node completes successfully, the current PostmortemState is persisted. If a node fails, the pipeline does not restart from the beginning — it retries from the last successful checkpoint.

If the Log Agent fails after 3 retry attempts, the Correlation Agent receives the state without log findings and acknowledges the missing signal. The postmortem is generated with a `signal_completeness: partial` flag.

**Design principle:** Partial postmortem with explicit uncertainty is more useful than no postmortem.

---

### 4.3 Jaeger is Unavailable

**Scenario:** Jaeger crashes. The Trace Agent cannot query trace data.

**Behavior:** The TelemetryQuerier's `query_traces()` catches the connection error and returns an empty result with an error flag. The Trace Agent reasons: "Trace data unavailable — cannot determine request flow." Postmortem includes a note about the gap.

---

### 4.4 Prometheus is Unavailable

**Scenario:** Prometheus is down. Metric Agent cannot run PromQL queries.

**Behavior:** Same pattern — empty results, flagged gap. Additionally, the gateway switches to log-based incident detection (counting ERROR-level log ingestion rate) as a degraded mode.

---

### 4.5 Gemini API Rate Limited or Unavailable

**Behavior:** Each LangGraph agent node uses exponential backoff with jitter: 1s → 2s + jitter → 4s + jitter → fail.

---

### 4.6 Telemetry Storm

**Behavior:** Token bucket rate limiter enforces per-service cap (capacity: 10,000 tokens, refill: 1,000 tokens/sec). Excess requests get `IngestionResponse{status: "rate_limited"}`. Service SDK backs off.

---

## 5. Performance Targets

### 5.1 End-to-End Incident Analysis Latency

| Milestone | Target |
|-----------|--------|
| Incident detected after threshold breach | < 10s |
| Agent pipeline triggered | < 2s |
| Triage Agent completes | < 15s |
| Parallel agents all complete | < 30s |
| Correlation + Root Cause | < 20s |
| Postmortem available | < 60s total |

### 5.2 Telemetry Ingestion Throughput

| Signal Type | Target |
|-------------|--------|
| Trace ingestion | 5,000 spans/sec |
| Log ingestion | 10,000 lines/sec |
| Metric ingestion | 2,000 data points/sec |

### 5.3 Concurrent Incident Handling

| Scenario | Target |
|----------|--------|
| Simultaneous analyses | 5 |
| Queue capacity | 20 |
| Max queue wait | < 30s |

### 5.4 Postmortem Quality

- Correct root cause service > 80% of runs
- Correct failure type > 70% of runs
- Always produce a report (never silent failure)

---

## 6. Design Tradeoffs

### 6.1 gRPC over REST for Telemetry Ingestion

**Decision:** gRPC for all telemetry ingestion and agent communication.

**Why not REST+JSON:** JSON serialization is ~5x larger than protobuf binary for traces with 50+ spans. JSON parsing is slower. HTTP/1.1 needs new connections; gRPC (HTTP/2) multiplexes streams.

**Cost:** Requires protobuf schema definitions and code generation. Slower to prototype. Accepted because interface contract clarity matters more than dev speed.

---

### 6.2 Token Bucket over Leaky Bucket

**Decision:** Token bucket for per-service rate limiting.

**Why not leaky bucket:** Leaky bucket enforces strictly uniform output — it penalizes bursts. Telemetry bursts during incidents are legitimate signals worth capturing. Token bucket allows bursts up to capacity while enforcing average rate.

**Leaky bucket would be preferred if:** Downstream had a hard queue limit. Future work considers this.

---

### 6.3 Parallel over Sequential Agent Execution

**Decision:** Trace, Log, Metric agents execute in parallel after Triage.

**Why not sequential:** Each queries different backends. Sequential would add 30–60s of unnecessary waiting. Parallel makes total time = duration of slowest agent, not sum.

**Cost:** Requires fan-in synchronization in LangGraph. Adds implementation complexity.

---

### 6.4 ChromaDB over Pinecone or Weaviate

**Decision:** ChromaDB (embedded) for RAG.

**Why ChromaDB:** Runs embedded — no external service, no API key, no network calls. Simpler docker-compose setup.

**At production scale:** Would switch to Weaviate or Pinecone. Captured in Future Work.

---

### 6.5 LangGraph over Raw LLM API Calls

**Decision:** LangGraph StateGraph for agent pipeline.

**Why not raw calls:** LangGraph provides state management (TypedDict), checkpointing (SQLite), conditional routing, and native tool integration. Raw calls would require building all of this from scratch.

**Cost:** Learning curve. Accepted because checkpointing and state management are architectural requirements.

---

### 6.6 SQLite Checkpointing over Redis or PostgreSQL

**Decision:** SQLite for LangGraph checkpointing.

**Why SQLite:** No additional service. Single-instance deployment. ~6 writes per analysis.

**When this reverses:** Multiple agent workers → switch to PostgresSaver (one-line config change).

---

## 8. Agent Pipeline Architecture

### Pipeline Structure

The agent pipeline is a LangGraph StateGraph with 7 nodes:

1. **Triage Agent** — Determines incident scope, severity, confirmed services
2. **Trace Analyzer** — Analyzes distributed traces for failure origin
3. **Log Correlator** — Analyzes error logs for patterns
4. **Metric Reasoner** — Analyzes Prometheus metrics for resource saturation
5. **Correlation Agent** — Synthesizes all findings into a causal chain
6. **Root Cause Agent** — Determines definitive root cause
7. **Postmortem Writer** — Generates structured markdown report

### Execution Flow

```
START → Triage → [SEV1/SEV2: Trace ‖ Log ‖ Metric → Correlation]
                → [SEV3: Correlation directly]
       → Root Cause → Postmortem Writer → END
```

### State Schema

The `PostmortemState` TypedDict (defined in `agents/state/postmortem_state.py`)
contains 33 fields organized by owning agent. Every field has a safe default.
No field is ever `None` or uninitialized.

Each agent owns its own field namespace:
- `triage_*` — Triage Agent
- `trace_*` — Trace Analyzer
- `log_*` — Log Correlator
- `metric_*` — Metric Reasoner
- `correlation_*` — Correlation Agent
- `root_cause*` — Root Cause Agent
- `postmortem_*` — Postmortem Writer

All agents append to `completed_agents`, `failed_agents`, and `errors`.

### Degraded Mode

Each analysis agent writes a `*_had_error` boolean flag. If set to `False`,
the Correlation Agent knows that signal is unavailable and adjusts confidence.
The pipeline never crashes on agent failure — it produces a partial report
with explicit data gaps noted.

### Parallelism

Trace, Log, and Metric agents run in parallel via LangGraph's `Send` API.
Wall-clock time = slowest agent (~20s), not sum (~55s).
Fan-in is automatic: Correlation agent starts only after all three complete.

### Checkpointing

SQLite checkpointer saves state after every node completion.
Thread ID = incident_id. Recovery resumes from last successful node.

### Source Files

| File | Purpose |
|------|---------|
| `agents/pipeline/DESIGN.md` | Full design specification with node contracts |
| `agents/pipeline/edges.py` | Conditional edge routing logic |
| `agents/pipeline/graph.py` | LangGraph StateGraph builder |
| `agents/state/postmortem_state.py` | PostmortemState TypedDict + initial_state() |
| `agents/triage/agent.py` | Triage agent stub (TODO-2.2) |
| `agents/trace_analyzer/agent.py` | Trace analysis agent stub |
| `agents/log_correlator/agent.py` | Log analysis agent stub |
| `agents/metric_reasoner/agent.py` | Metric analysis agent stub |
| `agents/correlation/agent.py` | Correlation agent stub |
| `agents/root_cause/agent.py` | Root cause agent stub |
| `agents/postmortem_writer/agent.py` | Postmortem writer agent stub |

---

## 9. Future Work

- **Kafka ingestion layer** — for production scale, replace direct gRPC with Kafka topics
- **Distributed agent worker pool** — multiple agent instances with consistent hashing routing
- **Gemini API quota management** — priority queue by incident severity
- **Multi-region deployment** — active-active to avoid analyzer being a single point of failure
- **Human-in-the-loop approval** — SRE reviews and can correct postmortem before publication; corrections feed back into ChromaDB as training data

---

*This document reflects the design as of initial implementation. All decisions are subject to revision as implementation reveals new constraints. Significant deviations from this document should be recorded with the rationale for the change.*
