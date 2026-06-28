import time
import logging
from datetime import datetime, timezone
from typing import Optional
from agents.server.incident_store import IncidentStore, SSEEvent

logger = logging.getLogger(__name__)

AGENT_DESCRIPTIONS = {
    "triage": "Assessing incident scope and severity",
    "parallel_fanout": "Starting parallel analysis",
    "trace_analyzer": "Analyzing distributed trace cascade path",
    "log_correlator": "Searching logs for error patterns",
    "metric_reasoner": "Scanning Prometheus metrics for resource saturation",
    "correlation": "Correlating signals and searching past incidents",
    "root_cause_analysis": "Determining root cause from evidence",
    "postmortem_writer": "Generating structured postmortem report",
    "store_postmortem": "Storing postmortem for future reference",
}


class PipelineObserver:
    def __init__(self, incident_id: str, store: IncidentStore):
        self.incident_id = incident_id
        self.store = store
        self._agent_start_times: dict = {}

    def on_pipeline_start(self):
        event = SSEEvent("pipeline_started", {
            "incident_id": self.incident_id,
            "message": "Ghost Debugger analysis pipeline started",
        })
        self._push_timeline({
            "type": "pipeline_started",
            "message": "Analysis pipeline started",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        self.store.sync_broadcast(self.incident_id, event)
        logger.info(f"[observer] pipeline started for {self.incident_id}")

    def on_agent_start(self, agent_name: str):
        self._agent_start_times[agent_name] = time.time()
        description = AGENT_DESCRIPTIONS.get(agent_name, f"Running {agent_name}")
        event = SSEEvent("agent_started", {
            "incident_id": self.incident_id,
            "agent_name": agent_name,
            "description": description,
        })
        self._push_timeline({
            "type": "agent_started",
            "agent_name": agent_name,
            "description": description,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        self.store.sync_broadcast(self.incident_id, event)

    def on_tool_called(self, agent_name: str, tool_name: str, args: dict):
        args_preview = {k: str(v)[:50] for k, v in args.items()}
        event = SSEEvent("tool_called", {
            "incident_id": self.incident_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "args_preview": args_preview,
        })
        self._push_timeline({
            "type": "tool_called",
            "agent_name": agent_name,
            "tool_name": tool_name,
            "args_preview": args_preview,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        self.store.sync_broadcast(self.incident_id, event)

    def on_tool_result(self, agent_name: str, tool_name: str, result_preview: str):
        event = SSEEvent("tool_result", {
            "incident_id": self.incident_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "result_preview": result_preview[:200],
        })
        self._push_timeline({
            "type": "tool_result",
            "agent_name": agent_name,
            "tool_name": tool_name,
            "result_preview": result_preview[:200],
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        self.store.sync_broadcast(self.incident_id, event)

    def on_agent_completed(self, agent_name: str, output_state: dict):
        duration_ms = 0
        if agent_name in self._agent_start_times:
            duration_ms = int((time.time() - self._agent_start_times[agent_name]) * 1000)

        key_finding = self._extract_key_finding(agent_name, output_state)

        event = SSEEvent("agent_completed", {
            "incident_id": self.incident_id,
            "agent_name": agent_name,
            "duration_ms": duration_ms,
            "key_finding": key_finding,
        })
        self._push_timeline({
            "type": "agent_completed",
            "agent_name": agent_name,
            "duration_ms": duration_ms,
            "key_finding": key_finding,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        self.store.sync_broadcast(self.incident_id, event)

    def on_agent_failed(self, agent_name: str, error: str):
        duration_ms = 0
        if agent_name in self._agent_start_times:
            duration_ms = int((time.time() - self._agent_start_times[agent_name]) * 1000)

        event = SSEEvent("agent_failed", {
            "incident_id": self.incident_id,
            "agent_name": agent_name,
            "duration_ms": duration_ms,
            "error": error[:200],
        })
        self._push_timeline({
            "type": "agent_failed",
            "agent_name": agent_name,
            "duration_ms": duration_ms,
            "error": error[:200],
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        self.store.sync_broadcast(self.incident_id, event)

    def on_pipeline_complete(self, final_state: dict):
        severity = final_state.get("triage_severity", "UNKNOWN")
        root_cause = final_state.get("root_cause", "")[:200]
        confidence = final_state.get("root_cause_confidence", 0.0)
        signal_completeness = final_state.get("signal_completeness", "unknown")
        completed = list(set(final_state.get("completed_agents", [])))
        failed = list(set(final_state.get("failed_agents", [])))
        report_len = len(final_state.get("postmortem_report", ""))

        event = SSEEvent("pipeline_complete", {
            "incident_id": self.incident_id,
            "severity": severity,
            "root_cause": root_cause,
            "confidence": confidence,
            "signal_completeness": signal_completeness,
            "completed_agents": completed,
            "failed_agents": failed,
            "report_length_chars": report_len,
        })
        self._push_timeline({
            "type": "pipeline_complete",
            "severity": severity,
            "root_cause": root_cause,
            "confidence": confidence,
            "signal_completeness": signal_completeness,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        self.store.sync_broadcast(self.incident_id, event)

    def on_pipeline_failed(self, error: str):
        event = SSEEvent("pipeline_failed", {
            "incident_id": self.incident_id,
            "error": error[:300],
        })
        self.store.sync_broadcast(self.incident_id, event)

    def _push_timeline(self, step: dict):
        self.store.sync_add_timeline_step(self.incident_id, step)

    def _extract_key_finding(self, agent_name: str, state: dict) -> str:
        if agent_name == "triage":
            severity = state.get("triage_severity", "UNKNOWN")
            services = state.get("triage_confirmed_services", [])
            return f"Severity: {severity} — Services: {', '.join(services) or 'none'}"

        elif agent_name == "trace_analyzer":
            first_svc = state.get("trace_first_error_service", "")
            cascade = state.get("trace_cascade_path", [])
            had_error = state.get("trace_had_error", False)
            if not had_error:
                return "Trace analysis unavailable (Jaeger unreachable)"
            cascade_str = " -> ".join(cascade) if cascade else "no cascade"
            return f"First error: {first_svc or 'unknown'} — Cascade: {cascade_str}"

        elif agent_name == "log_correlator":
            patterns = state.get("log_error_patterns", [])
            first_time = state.get("log_first_error_time", "")
            had_error = state.get("log_had_error", False)
            if not had_error:
                return "Log analysis unavailable"
            pattern_preview = patterns[0][:80] if patterns else "no patterns"
            return f"First error: {first_time or 'unknown'} — Pattern: {pattern_preview}"

        elif agent_name == "metric_reasoner":
            saturated = state.get("metric_saturated_resource", "")
            anomalies = state.get("metric_anomaly_details", [])
            had_error = state.get("metric_had_error", False)
            if not had_error:
                return "Metric analysis unavailable (Prometheus unreachable)"
            if saturated:
                return f"Saturated resource: {saturated}"
            return anomalies[0][:100] if anomalies else "No anomalies detected"

        elif agent_name == "correlation":
            chain = state.get("causal_chain", [])
            similar = state.get("similar_incidents", [])
            chain_preview = chain[0][:80] if chain else "no causal chain"
            similar_str = f" — Similar: {', '.join(similar[:2])}" if similar else ""
            return f"{chain_preview}{similar_str}"

        elif agent_name == "root_cause_analysis":
            rc = state.get("root_cause", "")[:120]
            conf = state.get("root_cause_confidence", 0.0)
            return f"[{conf:.0%} confidence] {rc}"

        elif agent_name == "postmortem_writer":
            completeness = state.get("signal_completeness", "unknown")
            report_len = len(state.get("postmortem_report", ""))
            return f"Report generated ({report_len} chars) — signals: {completeness}"

        return f"{agent_name} completed"
