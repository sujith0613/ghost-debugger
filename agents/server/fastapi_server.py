import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from agents.observability.metrics import active_analyses
from agents.server.incident_store import IncidentStore, SSEEvent
from agents.server.pipeline_observer import PipelineObserver
from agents.pipeline.graph import PipelineRunner
from agents.state.postmortem_state import initial_state

logger = logging.getLogger(__name__)

store = IncidentStore()
runner = PipelineRunner()

app = FastAPI(
    title="Ghost Debugger Agent Service",
    description="LangGraph-powered incident analysis pipeline",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    loop = asyncio.get_event_loop()
    store.set_loop(loop)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "ghost-debugger-agents",
        "active_analyses": int(active_analyses._value.get()),
    }


@app.get("/metrics")
async def prometheus_metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def dashboard():
    return FileResponse("agents/dashboard/index.html")


@app.post("/analyze")
async def analyze_incident(body: dict):
    incident_id = body.get("incident_id", "").strip()
    trigger_type = body.get("trigger_type", "manual")
    trigger_description = body.get("trigger_description", "")
    affected_services = body.get("affected_services", [])
    detected_at = body.get("detected_at", datetime.now(tz=timezone.utc).isoformat())
    analysis_window_seconds = body.get("analysis_window_seconds", 600)

    if not trigger_description:
        raise HTTPException(status_code=400, detail="trigger_description is required")
    if not isinstance(affected_services, list) or not affected_services:
        raise HTTPException(status_code=400, detail="affected_services must be a non-empty array")

    record = await store.create(
        trigger_type=trigger_type,
        trigger_description=trigger_description,
        affected_services=affected_services,
        detected_at=detected_at,
        analysis_window_seconds=analysis_window_seconds,
        incident_id=incident_id or None,
    )

    asyncio.create_task(_run_pipeline(record.incident_id, body))

    return {
        "incident_id": record.incident_id,
        "status": "queued",
        "message": f"Analysis started for {record.incident_id}",
    }


async def _run_pipeline(incident_id: str, body: dict):
    try:
        active_analyses.inc()

        observer = PipelineObserver(incident_id, store)
        observer.on_pipeline_start()

        state = initial_state(
            incident_id=incident_id,
            trigger_type=body.get("trigger_type", "manual"),
            trigger_description=body.get("trigger_description", ""),
            affected_services=body.get("affected_services", []),
            detected_at=body.get("detected_at", datetime.now(tz=timezone.utc).isoformat()),
            analysis_window_seconds=body.get("analysis_window_seconds", 600),
        )

        store.sync_update_status(incident_id, "running")

        config = {"configurable": {"thread_id": incident_id}}

        final_state = None
        for chunk in runner.pipeline.stream(state, config=config, stream_mode="updates"):
            for node_name, node_output in chunk.items():
                if node_name == "__end__":
                    continue

                # skip parallel_fanout (trivial lambda, no real work)
                if node_name == "parallel_fanout":
                    continue

                observer.on_agent_start(node_name)

                completed = node_output.get("completed_agents", [])
                failed = node_output.get("failed_agents", [])
                errors = node_output.get("errors", [])

                if node_name in completed:
                    observer.on_agent_completed(node_name, node_output)

                if node_name in failed:
                    error_msg = "; ".join(errors) if errors else f"{node_name} failed"
                    observer.on_agent_failed(node_name, error_msg)

                final_state = node_output

        snapshot = runner.pipeline.get_state(config)
        if snapshot:
            final_state = snapshot.values

        if final_state:
            store.sync_set_pipeline_state(incident_id, final_state)
            failed = final_state.get("failed_agents", [])
            if failed:
                store.sync_update_status(incident_id, "failed")
                observer.on_pipeline_failed(f"Agents failed: {', '.join(failed)}")
            else:
                store.sync_update_status(incident_id, "completed")
                observer.on_pipeline_complete(final_state)

    except Exception as e:
        logger.exception(f"Pipeline failed for {incident_id}")
        store.sync_update_status(incident_id, "failed")
        observer.on_pipeline_failed(str(e))
    finally:
        active_analyses.dec()


@app.get("/incidents")
async def list_incidents(status: Optional[str] = Query(None)):
    records = await store.list_all()
    summaries = [r.to_summary() for r in records]
    if status:
        summaries = [s for s in summaries if s["status"] == status]
    return {"incidents": summaries, "total": len(summaries)}


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    record = await store.get(incident_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    return record.to_detail()


@app.get("/incidents/{incident_id}/stream")
async def stream_incident(incident_id: str, request: Request):
    record = await store.get(incident_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

    sub = await store.subscribe(incident_id)
    if not sub:
        raise HTTPException(status_code=500, detail="Failed to create subscription")

    async def event_generator():
        try:
            async for event_wire in sub.events():
                if await request.is_disconnected():
                    break
                yield event_wire
        finally:
            sub.unsubscribe()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/incidents/{incident_id}/report")
async def download_report(incident_id: str):
    record = await store.get(incident_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

    state = record.pipeline_state or {}
    report_text = state.get("postmortem_report", "")
    if not report_text:
        raise HTTPException(status_code=404, detail="No postmortem report available yet")

    return Response(
        content=report_text,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f"attachment; filename=postmortem-{incident_id}.md",
        },
    )
