import logging
from typing import Optional
from langgraph.graph import StateGraph, END
# NOTE: spec calls for SqliteSaver but langgraph 0.2.28 doesn't ship
# langgraph.checkpoint.sqlite. MemorySaver provides same in-process resume.
from langgraph.checkpoint.memory import MemorySaver

from agents.state.postmortem_state import PostmortemState

from agents.pipeline.edges import (
    route_after_triage,
    fan_out_to_parallel_agents,
    route_after_postmortem,
)

from agents.triage.agent import triage_agent_node
from agents.trace_analyzer.agent import trace_analyzer_node
from agents.log_correlator.agent import log_correlator_node
from agents.metric_reasoner.agent import metric_reasoner_node
from agents.correlation.agent import correlation_agent_node
from agents.root_cause.agent import root_cause_agent_node
from agents.postmortem_writer.agent import postmortem_writer_node
from agents.pipeline.store_node import store_postmortem_node

logger = logging.getLogger(__name__)


def build_pipeline(
    checkpoint_db: str = ":memory:",
):
    checkpointer = MemorySaver()

    graph = StateGraph(PostmortemState)

    graph.add_node("triage", triage_agent_node)
    graph.add_node("trace_analyzer", trace_analyzer_node)
    graph.add_node("log_correlator", log_correlator_node)
    graph.add_node("metric_reasoner", metric_reasoner_node)
    graph.add_node("correlation", correlation_agent_node)
    # NOTE: spec uses "root_cause" but that's a state key (langgraph rejects it)
    graph.add_node("root_cause_analysis", root_cause_agent_node)
    graph.add_node("postmortem_writer", postmortem_writer_node)
    graph.add_node("store_postmortem", store_postmortem_node)

    graph.set_entry_point("triage")

    graph.add_conditional_edges(
        source="triage",
        path=route_after_triage,
        path_map={
            "parallel_analysis": "parallel_fanout",
            "correlation": "correlation",
            "postmortem_writer": "postmortem_writer",
        },
    )

    # NOTE: spec uses lambda state: {} but langgraph requires writing >=1 state key
    graph.add_node(
        "parallel_fanout",
        lambda state: {"completed_agents": ["parallel_fanout"]},
    )
    graph.add_conditional_edges(
        source="parallel_fanout",
        path=fan_out_to_parallel_agents,
    )

    graph.add_edge("trace_analyzer", "correlation")
    graph.add_edge("log_correlator", "correlation")
    graph.add_edge("metric_reasoner", "correlation")

    graph.add_edge("correlation", "root_cause_analysis")
    graph.add_edge("root_cause_analysis", "postmortem_writer")

    graph.add_conditional_edges(
        source="postmortem_writer",
        path=route_after_postmortem,
        path_map={
            "store_postmortem": "store_postmortem",
            "end": END,
        },
    )
    graph.add_edge("store_postmortem", END)

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("Ghost Debugger pipeline compiled successfully",
                extra={"nodes": list(graph.nodes)})

    return compiled


class PipelineRunner:

    def __init__(self, checkpoint_db: str = ":memory:"):
        self.pipeline = build_pipeline(checkpoint_db=checkpoint_db)
        self._checkpoint_db = checkpoint_db

    def run(
        self,
        incident_id: str,
        trigger_type: str,
        trigger_description: str,
        affected_services: list,
        detected_at: str,
        analysis_window_seconds: int = 600,
        stream_progress: bool = False,
    ) -> PostmortemState:
        from agents.state.postmortem_state import initial_state

        state = initial_state(
            incident_id=incident_id,
            trigger_type=trigger_type,
            trigger_description=trigger_description,
            affected_services=affected_services,
            detected_at=detected_at,
            analysis_window_seconds=analysis_window_seconds,
        )

        config = {
            "configurable": {
                "thread_id": incident_id,
            }
        }

        logger.info(f"Starting pipeline for incident {incident_id}")

        if stream_progress:
            return self._run_with_streaming(state, config)
        else:
            return self._run_blocking(state, config)

    def _run_blocking(self, state: PostmortemState, config: dict) -> PostmortemState:
        result = self.pipeline.invoke(state, config=config)
        logger.info(f"Pipeline complete: {result.get('completed_agents', [])}")
        return result

    def _run_with_streaming(
        self, state: PostmortemState, config: dict
    ) -> PostmortemState:
        final_state = None
        print(f"\n{'='*60}")
        print(f"  Ghost Debugger - Analyzing {state['incident_id']}")
        print(f"{'='*60}\n")

        for chunk in self.pipeline.stream(state, config=config, stream_mode="updates"):
            for node_name, node_output in chunk.items():
                if node_name == "__end__":
                    continue
                self._print_node_progress(node_name, node_output)
                final_state = node_output

        snapshot = self.pipeline.get_state(config)
        if snapshot:
            final_state = snapshot.values

        if final_state:
            print(f"\n{'='*60}")
            print(f"  Analysis Complete")
            print(f"  Severity:   {final_state.get('triage_severity', 'UNKNOWN')}")
            print(f"  Root Cause: {final_state.get('root_cause', 'Unknown')[:80]}")
            print(f"  Confidence: {final_state.get('root_cause_confidence', 0):.0%}")
            print(f"  Completed:  {final_state.get('completed_agents', [])}")
            print(f"  Failed:     {final_state.get('failed_agents', [])}")
            print(f"{'='*60}\n")

        return final_state or state

    def _print_node_progress(self, node_name: str, output: dict) -> None:
        node_icons = {
            "triage": "[TRIAGE]",
            "parallel_fanout": "[FAN-OUT]",
            "trace_analyzer": "[TRACE]",
            "log_correlator": "[LOG]",
            "metric_reasoner": "[METRIC]",
            "correlation": "[CORRELATE]",
            "root_cause": "[ROOT-CAUSE]",
            "postmortem_writer": "[WRITER]",
            "store_postmortem": "[STORE]",
        }
        icon = node_icons.get(node_name, "[NODE]")
        print(f"  {icon} {node_name} completed")

        if node_name == "triage":
            severity = output.get("triage_severity", "")
            services = output.get("triage_confirmed_services", [])
            if severity:
                print(f"       Severity: {severity}, Services: {services}")

        elif node_name == "trace_analyzer":
            first_svc = output.get("trace_first_error_service", "")
            if first_svc:
                print(f"       First error service: {first_svc}")

        elif node_name == "metric_reasoner":
            saturated = output.get("metric_saturated_resource", "")
            if saturated:
                print(f"       Saturated resource: {saturated}")

        elif node_name == "root_cause":
            rc = output.get("root_cause", "")
            conf = output.get("root_cause_confidence", 0)
            if rc:
                print(f"       Root cause: {rc[:80]}")
                print(f"       Confidence: {conf:.0%}")

        elif node_name == "postmortem_writer":
            report_len = len(output.get("postmortem_report", ""))
            completeness = output.get("signal_completeness", "")
            print(f"       Report: {report_len} chars, signals: {completeness}")

    def get_checkpoint(self, incident_id: str) -> Optional[dict]:
        config = {"configurable": {"thread_id": incident_id}}
        snapshot = self.pipeline.get_state(config)
        if snapshot and snapshot.values:
            return snapshot.values
        return None

    def resume(self, incident_id: str) -> Optional[PostmortemState]:
        existing = self.get_checkpoint(incident_id)
        if not existing:
            logger.warning(f"No checkpoint found for incident {incident_id}")
            return None

        completed = existing.get("completed_agents", [])
        if "postmortem_writer" in completed:
            logger.info(f"Pipeline for {incident_id} already complete - returning checkpoint")
            return existing

        logger.info(f"Resuming pipeline for {incident_id} from checkpoint. "
                    f"Completed nodes: {completed}")

        config = {"configurable": {"thread_id": incident_id}}
        result = self.pipeline.invoke(None, config=config)
        return result
