import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, AsyncIterator

logger = logging.getLogger(__name__)


class SSEEvent:
    def __init__(self, event_type: str, data: dict):
        self.event_type = event_type
        self.data = data
        self.timestamp = datetime.now(tz=timezone.utc).isoformat()

    def to_wire(self) -> str:
        payload = {"type": self.event_type, "timestamp": self.timestamp, **self.data}
        return f"event: {self.event_type}\ndata: {json.dumps(payload)}\n\n"


class IncidentRecord:
    def __init__(
        self,
        incident_id: str,
        trigger_type: str,
        trigger_description: str,
        affected_services: List[str],
        detected_at: str,
        analysis_window_seconds: int,
    ):
        self.incident_id = incident_id
        self.trigger_type = trigger_type
        self.trigger_description = trigger_description
        self.affected_services = affected_services
        self.detected_at = detected_at
        self.analysis_window_seconds = analysis_window_seconds

        self.status: str = "queued"
        self.created_at: str = datetime.now(tz=timezone.utc).isoformat()
        self.pipeline_state: Optional[dict] = None
        self.timeline: List[dict] = []
        self._subscribers: List[asyncio.Queue] = []

    def to_summary(self) -> dict:
        state = self.pipeline_state or {}
        return {
            "incident_id": self.incident_id,
            "trigger_description": self.trigger_description,
            "affected_services": self.affected_services,
            "detected_at": self.detected_at,
            "created_at": self.created_at,
            "status": self.status,
            "severity": state.get("triage_severity", "UNKNOWN"),
            "root_cause": (state.get("root_cause") or "")[:120],
            "root_cause_confidence": state.get("root_cause_confidence", 0.0),
            "signal_completeness": state.get("signal_completeness", "unknown"),
            "completed_agents": _dedup(state.get("completed_agents", [])),
            "failed_agents": _dedup(state.get("failed_agents", [])),
            "timeline_steps": len(self.timeline),
        }

    def to_detail(self) -> dict:
        state = self.pipeline_state or {}
        summary = self.to_summary()
        return {
            **summary,
            "trigger_type": self.trigger_type,
            "analysis_window_seconds": self.analysis_window_seconds,
            "timeline": self.timeline,
            "triage_findings": state.get("triage_findings", []),
            "triage_confirmed_services": state.get("triage_confirmed_services", []),
            "trace_findings": state.get("trace_findings", []),
            "trace_first_error_service": state.get("trace_first_error_service", ""),
            "trace_first_error_time": state.get("trace_first_error_time", ""),
            "trace_cascade_path": state.get("trace_cascade_path", []),
            "trace_had_error": state.get("trace_had_error", False),
            "log_findings": state.get("log_findings", []),
            "log_error_patterns": state.get("log_error_patterns", []),
            "log_first_error_time": state.get("log_first_error_time", ""),
            "log_had_error": state.get("log_had_error", False),
            "metric_findings": state.get("metric_findings", []),
            "metric_saturated_resource": state.get("metric_saturated_resource", ""),
            "metric_anomaly_details": state.get("metric_anomaly_details", []),
            "metric_had_error": state.get("metric_had_error", False),
            "correlation_summary": state.get("correlation_summary", ""),
            "causal_chain": state.get("causal_chain", []),
            "similar_incidents": state.get("similar_incidents", []),
            "contributing_factors": state.get("contributing_factors", []),
            "postmortem_report": state.get("postmortem_report", ""),
            "errors": state.get("errors", []),
        }

    async def broadcast(self, event: SSEEvent):
        dead = []
        for q in self._subscribers:
            try:
                await q.put(event)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    async def subscribe(self) -> "SSESubscription":
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return SSESubscription(q, self, self.incident_id)


class SSESubscription:
    def __init__(self, queue: asyncio.Queue, record: IncidentRecord, incident_id: str):
        self._queue = queue
        self._record = record
        self._incident_id = incident_id

    async def events(self) -> AsyncIterator[str]:
        for step in self._record.timeline:
            event = SSEEvent(step["type"], step)
            yield event.to_wire()

        while True:
            try:
                event: SSEEvent = await asyncio.wait_for(
                    self._queue.get(), timeout=30.0
                )
                yield event.to_wire()
                if event.event_type in ("pipeline_complete", "pipeline_failed"):
                    break
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                break

    def unsubscribe(self):
        try:
            self._record._subscribers.remove(self._queue)
        except ValueError:
            pass


class IncidentStore:
    def __init__(self):
        self._incidents: Dict[str, IncidentRecord] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def create(
        self,
        trigger_type: str,
        trigger_description: str,
        affected_services: List[str],
        detected_at: str,
        analysis_window_seconds: int,
        incident_id: Optional[str] = None,
    ) -> IncidentRecord:
        if not incident_id:
            ts = int(datetime.now(tz=timezone.utc).timestamp())
            short_id = uuid.uuid4().hex[:6].upper()
            incident_id = f"INC-{ts}-{short_id}"

        record = IncidentRecord(
            incident_id=incident_id,
            trigger_type=trigger_type,
            trigger_description=trigger_description,
            affected_services=affected_services,
            detected_at=detected_at,
            analysis_window_seconds=analysis_window_seconds,
        )

        async with self._lock:
            self._incidents[incident_id] = record

        logger.info(f"Created incident record: {incident_id}")
        return record

    def sync_update_status(self, incident_id: str, status: str):
        if incident_id in self._incidents:
            self._incidents[incident_id].status = status

    def sync_set_pipeline_state(self, incident_id: str, state: dict):
        if incident_id in self._incidents:
            self._incidents[incident_id].pipeline_state = state

    def sync_add_timeline_step(self, incident_id: str, step: dict):
        if incident_id in self._incidents:
            self._incidents[incident_id].timeline.append(step)

    def sync_broadcast(self, incident_id: str, event: SSEEvent):
        if self._loop and incident_id in self._incidents:
            asyncio.run_coroutine_threadsafe(
                self._incidents[incident_id].broadcast(event),
                self._loop,
            )

    async def get(self, incident_id: str) -> Optional[IncidentRecord]:
        return self._incidents.get(incident_id)

    async def list_all(self) -> List[IncidentRecord]:
        return sorted(
            self._incidents.values(),
            key=lambda r: r.created_at,
            reverse=True,
        )

    async def subscribe(self, incident_id: str) -> Optional["SSESubscription"]:
        record = self._incidents.get(incident_id)
        if not record:
            return None
        return await record.subscribe()


def _dedup(lst: list) -> list:
    seen = set()
    result = []
    for item in lst:
        if item not in result:
            result.append(item)
            seen.add(item)
    return result
