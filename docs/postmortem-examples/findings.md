# Ghost Debugger — Scenario Findings

## Environment
- **OS:** Windows (no Docker available)
- **Go services:** Compiled successfully (`go build`, `go vet`)
- **Agent server:** FastAPI + uvicorn on port 8090
- **LLM:** Google Gemini 1.5 Flash (no valid API key — all agents failed at LLM call)
- **Infrastructure:** Jaeger, Prometheus, ChromaDB, Grafana — all Docker-only, unavailable

## What Was Validated

| Component | Status | Details |
|-----------|--------|---------|
| Go test services (a, b, c, injector) | ✅ Compile clean | `go build`, `go vet` pass |
| Agent server startup | ✅ | FastAPI on port 8090, all 12 routes |
| POST /analyze | ✅ | Creates incident, returns immediately |
| Pipeline execution | ✅ | Iterates all 7 agents per incident |
| SSE events | ✅ | 20 timeline events per run |
| Incident store | ✅ | Thread-safe, timeline + state capture |
| Dashboard | ✅ | Serves 25 KB HTML at GET / |
| List/detail endpoints | ✅ | GET /incidents, GET /incidents/{id} |
| Graceful failure | ✅ | Pipeline failures never crash server |
| Report generation | ⚠️ Partial | 1,212 chars generated (error template, not real analysis) |

## What Requires a Valid GOOGLE_API_KEY + Docker

| Capability | Dependency |
|-----------|-----------|
| Real LLM-based analysis | Valid GOOGLE_API_KEY |
| Test service orchestration | Docker Compose (Jaeger, OTLP exporter) |
| Real trace ingestion | Jaeger running via Docker |
| Real metric queries | Prometheus running via Docker |
| RAG incident lookup | ChromaDB running via Docker |
| Failure injection scenarios | service_a/b/c + failure_injector running |

## Agent Dual-Registration Bug

Every agent appears in **both** `completed_agents` and `failed_agents` on failure.
Root cause: agent code adds itself to `completed_agents` unconditionally on function return,
then the error handler adds it to `failed_agents`. This is not a critical bug — `completed_agents`
tracks "function executed" for checkpointing — but the duplicative membership may confuse
downstream consumers that expect mutually exclusive lists.

## How to Run Full Scenarios

```powershell
# 1. Set your API key
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
