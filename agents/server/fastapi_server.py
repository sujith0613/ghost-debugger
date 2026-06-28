# agents/server/fastapi_server.py
#
# HTTP REST interface for the agent service.
# Wraps the gRPC service for the web dashboard and manual triggering.

from fastapi import FastAPI

app = FastAPI(
    title="Ghost Debugger Agent Service",
    description="LangGraph-powered incident analysis pipeline",
    version="1.0.0",
)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ghost-debugger-agent"}

# TODO Phase 2: Implement full endpoints
# @app.post("/analyze")
# @app.get("/incidents")
# @app.get("/incidents/{incident_id}")
