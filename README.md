# Ghost Debugger

**AI-Powered Distributed System Postmortem Analyzer.**

Ghost Debugger ingests OpenTelemetry traces, Prometheus metrics, and structured logs from instrumented microservices via a Go gRPC gateway, then runs a LangGraph multi-agent pipeline to automatically generate structured postmortem reports — root cause, timeline, blast radius, action items — in under 60 seconds.

---

## Problem

When a distributed system fails, signals fragment across three isolated tools: traces in Jaeger, metrics in Prometheus, logs in files. An SRE responding to a PagerDuty alert must manually correlate all three by timestamp, determine which service failed first and why, then write a postmortem from scratch. This takes **45 minutes to 4 hours** depending on incident complexity.

Ghost Debugger reduces this to **under 60 seconds** of automated analysis with a human reviewing the output — closing the gap between signal availability and actionable intelligence.

---

## Architecture

```
Test Services (OTel-instrumented)
    │ gRPC (traces/metrics/logs)
    ▼
Go Telemetry Gateway  ──►  Jaeger (traces)
  (rate limiter,            Prometheus (metrics)
   circuit breaker,         ChromaDB (RAG)
   incident detector)            │
    │ gRPC                       │ queried by agents
    ▼                            ▼
Python Agent Service (LangGraph)
  Triage → [Trace‖Log‖Metric] → Correlation → Root Cause → Postmortem Writer
    │
    ▼
FastAPI + Web Dashboard
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design document, including component interfaces, failure mode analysis, and complete tradeoff rationale.

---

## Quick Start

```bash
# Clone and start everything
git clone https://github.com/sujithm/ghost-debugger.git
cd ghost-debugger
docker-compose up --build
```

This starts all 9 services:

| Service | Port | Purpose |
|---------|------|---------|
| Jaeger | `16686` | Trace storage & UI |
| Prometheus | `9091` | Metric storage |
| ChromaDB | `8000` | Vector store for RAG |
| Gateway | `9000` (gRPC), `9090` (HTTP) | Telemetry ingestion |
| Agents | `9001` | LangGraph pipeline |
| service_a | `8081` | Test microservice |
| service_b | `8082` | Test microservice |
| service_c | `8083` | Test microservice |
| failure_injector | `8084` | Chaos control plane |

---

## Demo

### 1. Generate traffic to test services

```bash
# Send a request through the 3-service chain
curl http://localhost:8081/process
```

### 2. Inject a failure

```bash
# Make service_a slow — triggers a cascade timeout
curl -X POST http://localhost:8084/inject \
  -H "Content-Type: application/json" \
  -d '{"target":"service_a","latency_ms":5000,"duration_s":30}'
```

### 3. View the postmortem

```bash
# Trigger analysis
curl -X POST http://localhost:8090/analyze \
  -H "Content-Type: application/json" \
  -d '{"incident_id":"cascade-001"}'

# Returns structured markdown with root cause, timeline, blast radius, action items
```

### 4. Inspect traces in Jaeger

Open `http://localhost:16686` — the failure trace shows `service_a` spans with 5s latency propagating timeouts to `service_b` and `service_c`.

---

## Engineering Decisions

All tradeoffs are documented in [`ARCHITECTURE.md §6`](ARCHITECTURE.md#6-design-tradeoffs).

| Decision | Rationale |
|----------|-----------|
| **gRPC over REST** | Protobuf binary ~5x smaller than JSON; HTTP/2 multiplexing; typed interface contracts |
| **Token bucket over leaky bucket** | Allows bursts during incidents (when telemetry matters most) while enforcing average rate |
| **Parallel over sequential agents** | Each agent queries independent backends — parallel cuts analysis time from sum to max |
| **ChromaDB over Pinecone** | Embedded — no external API key, no network latency, simpler deployment |
| **LangGraph over raw LLM calls** | State management, checkpointing, conditional routing, native tool integration |
| **SQLite over Redis** | Single-instance — no additional service; one-line migration to PostgresSaver at scale |

---

## Performance Targets

| Metric | Target |
|--------|--------|
| End-to-end incident analysis | < 60 seconds |
| Trace ingestion throughput | 5,000 spans/sec |
| Log ingestion throughput | 10,000 lines/sec |
| Metric ingestion throughput | 2,000 data points/sec |
| Concurrent incident analyses | 5 |
| Correct root cause service | > 80% of runs |

---

## Status

- **Phase 0 (Foundation):** ✅ Complete — architecture, gRPC, OTel, LangGraph fundamentals, monorepo, docker-compose
- **Phase 1 (Infrastructure):** 🟡 In progress — gateway, protos, service_a done; service_b/c/failure_injector pending; agent pipeline not yet built
- **Phase 2+ (Agents, Observability, Docs):** ⬜ Not started

---

## Why This Matters for Interviews

Ghost Debugger is designed to demonstrate systems thinking, not just feature completion. Every component exists because of an explicit engineering decision with a documented alternative and rationale. The design doc ([ARCHITECTURE.md](ARCHITECTURE.md)) and tradeoff analysis are written to be discussed in interviews — not as a tutorial walkthrough, but as evidence of deliberate architectural judgment.

Key talking points:
- **Distributed systems:** gRPC, OpenTelemetry, circuit breakers, rate limiting, W3C TraceContext propagation
- **Concurrency:** Go goroutine worker pools, token bucket algorithm, fan-in/fan-out agent orchestration
- **AI + systems intersection:** LangGraph multi-agent reasoning over real telemetry data, RAG over historical incidents
- **Resilience:** Every component has a documented failure mode — partial results are preferred over silent failures
