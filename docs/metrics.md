# Ghost Debugger - Metrics Reference

Every metric answers a question. This document states the question,
the normal range, the alert threshold, and the action to take.

---

## Gateway Metrics

### ghost_debugger_gateway_ingestion_total
**Question:** How many telemetry events is the gateway processing?
**Labels:** service, signal_type (trace/log/metric), status (accepted/rate_limited/error)
**Normal:** Steady rate matching service activity
**Alert:** rate_limited > 100/min for any service -> telemetry bug in that service
**Alert:** error > 10/min -> storage backend failing
**Action:** Check service logs for telemetry emission bugs; check Jaeger/storage health

### ghost_debugger_gateway_ingestion_duration_seconds
**Question:** How fast is the gateway processing each ingestion request?
**Normal:** p99 < 1ms (the gateway path is synchronous and lightweight)
**Alert:** p99 > 10ms -> worker pool or storage is backed up
**Action:** Check worker_pool_queue_depth; increase WORKER_COUNT if queue is growing

### ghost_debugger_gateway_circuit_breaker_state
**Question:** Is the agent service reachable?
**Values:** 0=CLOSED (healthy), 1=OPEN (agent unreachable), 2=HALF-OPEN (recovering)
**Normal:** 0 (CLOSED) at all times
**Alert:** 1 (OPEN) for > 60 seconds -> agent service is down
**Action:** Check docker compose logs agents; check GOOGLE_API_KEY; check Gemini API status

### ghost_debugger_gateway_worker_pool_dropped_total
**Question:** Is the gateway losing telemetry?
**Normal:** 0 at all times - any drop is a problem
**Alert:** Any non-zero value -> telemetry loss occurring
**Action:** Increase WORKER_COUNT; check if Jaeger is slow (backing up workers)

### ghost_debugger_gateway_active_incidents
**Question:** How many incidents are being analyzed right now?
**Normal:** 0-3
**Alert:** > 10 -> agent pipeline is overwhelmed or stuck
**Action:** Check agent service for hung goroutines; check Gemini API response times

---

## Agent Metrics

### ghost_debugger_agent_node_duration_seconds
**Question:** Which agent is the bottleneck in the pipeline?
**Normal per agent:**
  - triage: p99 < 25s
  - trace_analyzer: p99 < 30s
  - log_correlator: p99 < 25s
  - metric_reasoner: p99 < 35s
  - correlation: p99 < 20s
  - root_cause: p99 < 15s
  - postmortem_writer: p99 < 15s
**Alert:** Any agent p99 > 45s -> investigate LLM calls for that agent
**Action:** Check llm_call_duration_seconds for the slow agent; check Gemini API

### ghost_debugger_agent_node_total
**Question:** Are agents succeeding or failing?
**Normal:** failed/total < 5% for all agents
**Alert:** failed/total > 10% for any agent -> broken tools or LLM errors
**Action:** Check agent logs for QueryError; check storage backend health

### ghost_debugger_llm_call_duration_seconds
**Question:** Is the Gemini API responding normally?
**Normal:** p50 < 5s, p99 < 15s
**Alert:** p99 > 20s -> Gemini API is degraded
**Action:** Check https://status.cloud.google.com; switch to gemini-1.5-flash if on pro

### ghost_debugger_incident_analysis_duration_seconds
**Question:** How long does a full incident analysis take?
**Target:** p99 < 90s
**Alert:** p99 > 120s -> pipeline is too slow for real-time use
**Action:** Identify slowest agent via node_duration_seconds; check Gemini latency

### ghost_debugger_signal_completeness_total
**Question:** How often do we have all three observability signals available?
**Normal:** "full" > 95% of analyses
**Alert:** "partial" > 30% -> storage backends are flaky
**Action:** Check Jaeger, Prometheus, ChromaDB health; check docker compose ps

### ghost_debugger_chromadb_query_duration_seconds
**Question:** How fast are RAG similarity searches?
**Normal:** p99 < 500ms
**Alert:** p99 > 2s -> ChromaDB index issue or high collection size
**Action:** Check ChromaDB container resources; consider index optimization

---

## Reading the Dashboard

The Grafana dashboard has three rows:

**Row 1 - Gateway Health:**
Check this first when Ghost Debugger seems unresponsive.
Circuit breaker state is the most important single metric.

**Row 2 - Agent Pipeline Performance:**
Check this when analyses are slow or failing.
The bargauge shows relative agent durations - the longest bar is the bottleneck.

**Row 3 - Incident Analysis:**
Check this to understand Ghost Debugger's activity and effectiveness.
High partial signal rate indicates infrastructure issues to address.
