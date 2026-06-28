from prometheus_client import Counter, Histogram, Gauge

agent_node_duration_seconds = Histogram(
    "ghost_debugger_agent_node_duration_seconds",
    "Duration of each agent node execution in seconds. "
    "Alert on any agent p99 > 45s (LLM timeout risk).",
    labelnames=["agent_name"],
    buckets=[1, 5, 10, 15, 20, 30, 45, 60, 90, 120],
)

agent_node_total = Counter(
    "ghost_debugger_agent_node_total",
    "Total agent node executions by status. "
    "Alert on failed/total > 10% for any agent.",
    labelnames=["agent_name", "status"],
)

agent_tool_calls_total = Counter(
    "ghost_debugger_agent_tool_calls_total",
    "Total tool calls made by agents. "
    "High rate = agents are making many queries (may be inefficient).",
    labelnames=["agent_name", "tool_name", "status"],
)

agent_tool_duration_seconds = Histogram(
    "ghost_debugger_agent_tool_duration_seconds",
    "Duration of tool calls from agents. "
    "Alert on query_traces or query_metrics p99 > 5s (backend slow).",
    labelnames=["tool_name"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

llm_call_duration_seconds = Histogram(
    "ghost_debugger_llm_call_duration_seconds",
    "Duration of Gemini API calls. "
    "Alert on p99 > 20s (Gemini API degraded).",
    labelnames=["agent_name"],
    buckets=[1, 2, 5, 10, 15, 20, 30, 60],
)

llm_call_total = Counter(
    "ghost_debugger_llm_call_total",
    "Total Gemini API calls by status. "
    "Alert on rate_limited > 10/min (quota exhaustion).",
    labelnames=["agent_name", "status"],
)

llm_tokens_total = Counter(
    "ghost_debugger_llm_tokens_total",
    "Approximate token usage by agent (input + output). "
    "Used for cost estimation and quota monitoring.",
    labelnames=["agent_name", "direction"],
)

incident_analysis_duration_seconds = Histogram(
    "ghost_debugger_incident_analysis_duration_seconds",
    "End-to-end incident analysis duration from trigger to postmortem. "
    "Alert on p99 > 90s.",
    buckets=[10, 20, 30, 45, 60, 75, 90, 120, 180, 300],
)

incident_analysis_total = Counter(
    "ghost_debugger_incident_analysis_total",
    "Total incident analyses by outcome. "
    "Alert on failed/total > 20%.",
    labelnames=["status", "severity"],
)

active_analyses = Gauge(
    "ghost_debugger_active_analyses",
    "Number of incident analyses currently running. "
    "Alert on > 5 (Gemini quota pressure).",
)

signal_completeness_total = Counter(
    "ghost_debugger_signal_completeness_total",
    "Pipeline completions by signal availability. "
    "High partial rate = storage backends are flaky.",
    labelnames=["completeness"],
)

chromadb_query_duration_seconds = Histogram(
    "ghost_debugger_chromadb_query_duration_seconds",
    "ChromaDB similarity search duration. "
    "Alert on p99 > 2s (embedding or index issue).",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)

chromadb_results_returned = Histogram(
    "ghost_debugger_chromadb_results_returned",
    "Number of similar incidents returned per RAG query. "
    "Consistently 0 = collection is empty or threshold too high.",
    buckets=[0, 1, 2, 3, 5],
)

postmortem_stored_total = Counter(
    "ghost_debugger_postmortem_stored_total",
    "Total postmortems stored in ChromaDB. "
    "Tracks growth of incident knowledge base.",
    labelnames=["status"],
)


class AgentMetricsRecorder:
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._start = None
        self._status = "success"

    def __enter__(self):
        import time
        self._start = time.time()
        active_analyses.inc()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        import time
        duration = time.time() - self._start

        if exc_type is not None:
            self._status = "failed"

        agent_node_duration_seconds.labels(
            agent_name=self.agent_name
        ).observe(duration)

        agent_node_total.labels(
            agent_name=self.agent_name,
            status=self._status,
        ).inc()

        active_analyses.dec()
        return False

    def record_tool_call(self, tool_name: str, success: bool, duration: float):
        status = "success" if success else "error"
        agent_tool_calls_total.labels(
            agent_name=self.agent_name,
            tool_name=tool_name,
            status=status,
        ).inc()
        agent_tool_duration_seconds.labels(tool_name=tool_name).observe(duration)

    def record_llm_call(self, success: bool, duration: float, status: str = "success"):
        llm_call_duration_seconds.labels(agent_name=self.agent_name).observe(duration)
        llm_call_total.labels(agent_name=self.agent_name, status=status).inc()

    def set_status(self, status: str):
        self._status = status


class PipelineMetricsRecorder:
    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self._start = None

    def __enter__(self):
        import time
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def record_complete(self, status: str, severity: str, completeness: str):
        import time
        duration = time.time() - self._start

        incident_analysis_duration_seconds.observe(duration)
        incident_analysis_total.labels(status=status, severity=severity).inc()
        signal_completeness_total.labels(completeness=completeness).inc()
