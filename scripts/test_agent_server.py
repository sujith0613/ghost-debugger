"""Minimal AgentService server for cross-language gRPC verification."""
import sys
import time
from concurrent import futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents" / "proto"))

import grpc
import agent_pb2
import agent_pb2_grpc


class TestAgentServicer(agent_pb2_grpc.AgentServiceServicer):
    def AnalyzeIncident(self, request, context):
        print(f"[agent] received incident: {request.incident_id}")
        print(f"[agent] trigger: {request.trigger_type} - {request.trigger_description}")
        print(f"[agent] services: {list(request.services)}")

        return agent_pb2.AnalysisResponse(
            incident_id=request.incident_id,
            findings=[
                agent_pb2.AgentFinding(
                    agent_name="triage",
                    finding="Detected anomaly in request flow across 3 services",
                    confidence=0.87,
                    completed_at_ns=time.time_ns(),
                ),
                agent_pb2.AgentFinding(
                    agent_name="trace",
                    finding="Cascade failure pattern: service_a latency spike propagated to service_b",
                    confidence=0.92,
                    completed_at_ns=time.time_ns(),
                ),
            ],
            root_cause="service_a: database connection pool exhaustion",
            root_cause_confidence=0.89,
            postmortem_markdown="# Postmortem Report\n\n## Root Cause\nservice_a database connection pool exhaustion\n\n## Timeline\n...",
            similar_incidents=["incident-2024-11-03", "incident-2025-01-15"],
            analysis_duration_ms=3421,
            status="completed",
        )


def serve():
    port = "9001"
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    agent_pb2_grpc.add_AgentServiceServicer_to_server(TestAgentServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    print(f"[agent] test server listening on port {port}")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
