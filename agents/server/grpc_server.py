# agents/server/grpc_server.py
#
# gRPC server that exposes the LangGraph agent pipeline.
# The Go gateway calls this server when an incident is detected.
#
# Implements AgentService from proto/agent.proto:
#   - AnalyzeIncident: unary call, returns complete postmortem
#   - AnalyzeIncidentWithProgress: server streaming, sends agent progress updates

# TODO Phase 2: Implement gRPC server
# from concurrent import futures
# import grpc
# from proto import agent_pb2, agent_pb2_grpc
# from agents.pipeline import build_pipeline
