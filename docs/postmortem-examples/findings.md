# Ghost Debugger — Scenario Findings

## Environment
- **OS:** Windows (no Docker available)
- **Go services:** Compiled successfully (go build, go vet)
- **Agent server:** FastAPI + uvicorn on port 8090
- **LLM:** Ollama qwen2.5:1.5b (local, CPU)
- **Infrastructure:** Jaeger, Prometheus, ChromaDB, Grafana — all Docker-only, unavailable

## What Was Validated

| Component | Status | Details |
|-----------|--------|---------|
| Go test services (a, b, c, injector) | Compile clean | go build, go vet pass |
| Agent server startup | Working | FastAPI on port 8090, all 12 routes |
| POST /analyze | Working | Creates incident, returns immediately |
| Pipeline execution | Working | Iterates all 7 agents per incident |
| SSE events | Working | 20 timeline events per run |
| Incident store | Working | Thread-safe, timeline + state capture |
| Dashboard | Working | Serves 25 KB HTML at GET / |
| List/detail endpoints | Working | GET /incidents, GET /incidents/{id} |
| Graceful failure | Working | Pipeline failures never crash server |

## Known Issue: No-Data Hallucination

When triggered without real telemetry (no Docker, no services running),
the LLM receives empty tool results and fabricates plausible-sounding data:

- "unknown_service" appears in cascade path (no traces exist)
- "Service 1, Service 2, Service 3" as metric labels (actual names: service_a/b/c)
- All timestamps identical to detection time (no real events found)
- Confidence 1.0 on completely invented narrative (no uncertainty guardrail)

**Root cause:** Empty tool results -> LLM fills in plausible numbers ->
internally consistent fake narrative -> high confidence.

**Fix applied:** Preflight data check in triage agent detects empty backends
before the LLM runs. If no telemetry exists, triage returns early with
empty confirmed_services, which routes directly to postmortem_writer where
a "Cannot analyze" report template is used instead of running the LLM.

## What Requires a Valid GOOGLE_API_KEY + Docker

| Capability | Dependency |
|-----------|-----------|
| Real LLM-based analysis | Valid GOOGLE_API_KEY or larger local model (7B+) |
| Test service orchestration | Docker Compose (Jaeger, OTLP exporter) |
| Real trace ingestion | Jaeger running via Docker |
| Real metric queries | Prometheus running via Docker |
| RAG incident lookup | ChromaDB running via Docker |
| Failure injection scenarios | service_a/b/c + failure_injector running |

## How to Run Full Scenarios

```powershell
# 1. Set your API key (or rely on Ollama with a larger model)
$env:GOOGLE_API_KEY = "your-actual-key"

# 2. Start infrastructure + services (requires Docker Desktop)
docker compose --profile services --profile app up -d

# 3. Verify everything is running
.\scripts\preflight_check.ps1

# 4. Run all three scenarios
.\scripts\run_scenario.ps1 -Scenario all

# 5. Analyze results
python3 scripts\analyze_results.py docs\postmortem-examples\
```
