# pingpong/python_server.py
#
# Python gRPC server implementing AgentService.Ping
# Run this first, then run the Go client.

import sys
import os
import time
from concurrent import futures

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import grpc
from agents.proto import agent_pb2
from agents.proto import agent_pb2_grpc


class PingPongAgentService(agent_pb2_grpc.AgentServiceServicer):

    def Ping(self, request, context):
        received_at = int(time.time() * 1000)
        replied_at = int(time.time() * 1000)
        return agent_pb2.PongResponse(
            original_message=request.message,
            server_language="Python",
            received_at=received_at,
            replied_at=replied_at,
            round_trip_ms=replied_at - request.sent_at,
        )

    def AnalyzeIncident(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("AnalyzeIncident not implemented in ping-pong test server")
        return agent_pb2.AnalysisResponse()

    def AnalyzeIncidentWithProgress(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("AnalyzeIncidentWithProgress not implemented")
        return


def serve():
    port = "50099"
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=5))
    agent_pb2_grpc.add_AgentServiceServicer_to_server(PingPongAgentService(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"[python-server] Ping-pong server listening on port {port}")
    print(f"[python-server] Waiting for Go client to call Ping...")
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print("[python-server] Stopping.")
        server.stop(grace=2)


if __name__ == "__main__":
    serve()
