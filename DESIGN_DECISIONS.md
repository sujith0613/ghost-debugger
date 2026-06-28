# Design Decisions Log

Every significant decision made during implementation is recorded here.
Format: decision made → alternatives considered → why this choice → consequences.

This document exists because:
1. Future-you will forget why a decision was made
2. Interviewers will ask "why did you choose X over Y"
3. This is how Google engineers document engineering judgment

---

## DDR-001: Go for Gateway, Python for Agents

**Decision:** Go handles all telemetry ingestion and routing. Python handles all agent logic.

**Alternatives considered:**
- All Go: possible with Go LLM clients, but LangGraph ecosystem is Python-native
- All Python: FastAPI could handle ingestion, but Go's concurrency model (goroutines, channels) is significantly better for high-throughput I/O
- Node.js for gateway: good async I/O but weaker type system, no goroutines, smaller systems ecosystem

**Why this choice:**
Go's goroutine-per-request model handles thousands of concurrent telemetry ingestion calls with minimal overhead. Each gRPC call from a test service gets its own goroutine — the scheduler multiplexes these onto OS threads automatically. Python's GIL would serialize these calls, turning a concurrency problem into a throughput bottleneck at exactly the wrong layer.

LangGraph is Python-native. Its StateGraph, Send() API, checkpointing, and tool binding are all built around Python's type system and async model. Building the agent layer in Go would require either reimplementing LangGraph in Go or writing a Go-native agent framework — cost with no benefit, since the agent layer is not in the hot path for throughput.

The boundary is clean: Go owns the I/O-bound, high-throughput, latency-sensitive ingestion path. Python owns the compute-bound, LLM-driven, latency-tolerant analysis path.

**Consequences:**
Cross-language communication requires gRPC and shared protobuf definitions. The proto files become the API contract — a service boundary that is enforced by the compiler rather than by convention. A code generation step (`./scripts/gen_proto.sh`) is added to the build process. Developers must install both Go and Python toolchains. Docker images for the two services are built from different base images. The payoff is that each language is doing exactly what it is best at.

---

## DDR-002: gRPC over REST for Inter-Service Communication

**Decision:** All service-to-service communication uses gRPC + protobuf.

**Alternatives considered:**
- REST/JSON: simpler to debug (curl-able), universally understood, but 5× larger payloads and slower parsing at volume
- GraphQL: unnecessary complexity — the query surface is fixed, not dynamic
- MessagePack over HTTP: binary encoding but no streaming, no generated clients, no deadline propagation
- Apache Thrift: similar to gRPC but smaller ecosystem and weaker Go support

**Why this choice:**
Three costs make REST/JSON the wrong choice at telemetry ingestion volume:

*Serialization cost.* A trace with 50 spans serialized to JSON is approximately 5× larger than the same data in protobuf binary format. At 10,000 ingestion events per second, this is the difference between 50MB/s and 10MB/s of wire traffic — a real constraint on a single-machine deployment.

*Parsing cost.* JSON parsing requires string scanning, unicode validation, and dynamic type inference. Protobuf deserialization locates fields by fixed byte offsets. The CPU cost difference is measurable at high throughput.

*Connection overhead.* HTTP/1.1 creates a new TCP connection per request (or requires explicit keep-alive management). gRPC runs over HTTP/2 with multiplexed streams — all three test services share one TCP connection to the gateway simultaneously, with no connection establishment overhead per request.

Beyond performance: the `.proto` file is the canonical API contract. When a service sends a field that doesn't exist in the schema, the error surfaces at code generation time — not as a silent null in production. Adding a required field to a message causes compilation failures in all consumers immediately, not runtime surprises.

**Consequences:**
gRPC requires `protoc` and language-specific plugins to generate client/server stubs. Debugging gRPC calls requires `grpcurl` rather than `curl`. The cross-language ping-pong test (Go client → Python server) was required to verify that protobuf binary format is compatible across both generated codebases before any real logic was written. These are real costs accepted in exchange for type safety, performance, and streaming support.

---

## DDR-003: Token Bucket over Leaky Bucket for Rate Limiting

**Decision:** Per-service token bucket rate limiter with 10,000-token capacity and 1,000 tokens/second refill rate.

**Alternatives considered:**
- Leaky bucket: strictly uniform output rate, queues or drops burst arrivals
- Fixed window counter: simple but suffers from boundary spike — 2× allowed rate possible at window edges
- Sliding window log: most accurate, but O(requests) memory per service — doesn't scale
- Token bucket via `golang.org/x/time/rate`: correct implementation but hides the algorithm; interview value requires understanding the internals

**Why this choice:**
The choice between token bucket and leaky bucket comes down to one question: *are bursts legitimate signals or noise?*

For telemetry ingestion, bursts are legitimate and high-value. When service_b starts failing, it emits a burst of error spans, error logs, and anomalous metrics simultaneously. This burst is exactly the data needed for root cause analysis — it is the incident signal. A leaky bucket would smooth this burst, enforcing a uniform drain rate and potentially discarding the most valuable telemetry at the worst possible moment.

Token bucket allows bursts up to the capacity (10,000 tokens), then enforces an average rate ceiling through the refill rate (1,000/second). A misbehaving service that sends continuously will exhaust its bucket and be rate-limited, but a well-behaved service experiencing an incident spike will have its burst absorbed.

The implementation uses lazy refill — tokens are not refilled on a background goroutine (which would require one goroutine per service, not scalable to thousands of services). Instead, tokens refill on each `Allow()` call by computing `elapsed_seconds × refill_rate`. This is the same approach as `golang.org/x/time/rate` and avoids timer overhead entirely.

**Consequences:**
Token bucket allows a service to send 10,000 events instantly if the bucket is full. This is intentional but must be understood by operators: a service recovering from a crash can send a burst of backlogged telemetry without being immediately rate-limited. The sliding-window failure counting in the incident detector complements this — a burst of errors is detected as an incident, triggering analysis, rather than being smoothed away.

---

## DDR-004: ChromaDB over Pinecone or Weaviate for RAG

**Decision:** ChromaDB running in embedded mode as the vector store for postmortem RAG retrieval.

**Alternatives considered:**
- Pinecone: managed cloud service, strong performance, but requires API key, external dependency, network latency per query, paid service at scale
- Weaviate: production-grade, feature-rich, but runs as a separate service (adds one more container), more complex configuration
- pgvector (PostgreSQL extension): already have PostgreSQL in the stack, but requires running a separate PG instance or sharing the application DB — mixing operational and vector data in one database is an antipattern
- FAISS (Facebook AI Similarity Search): excellent performance but requires writing the persistence layer manually; ChromaDB uses FAISS internally
- Qdrant: strong performance, Rust-based, but adds another language runtime to understand and debug

**Why this choice:**
ChromaDB runs embedded — it operates inside the Python process with no external service, no API key, and no network calls for local deployments. This reduces the docker-compose stack by one container and eliminates network latency on every RAG query during the correlation agent's operation.

The `hnsw:space=cosine` metadata setting configures HNSW (Hierarchical Navigable Small World) graph indexing with cosine similarity — correct for text embeddings where magnitude is not meaningful, only direction. ChromaDB handles the embedding model selection and HNSW index management transparently.

For testing, ChromaDB's `EphemeralClient()` creates an in-memory instance that requires no running server, which made the 6 ChromaDB unit tests fast and side-effect-free.

**When this decision reverses:**
At production scale — millions of stored postmortems, sub-10ms similarity search requirements, multiple agent workers querying simultaneously — ChromaDB's embedded model becomes a contention bottleneck (single-process, GIL-affected). The migration path is Weaviate or Qdrant with HTTP API — a one-file change in `chromadb_client.py`. The interface abstraction (`TelemetryQuerier.search_similar_incidents`) means no agent code changes are required.

**Consequences:**
ChromaDB's embedded mode means the vector index lives in the agent service process. Multiple agent service instances cannot share the same embedded ChromaDB — each would have its own independent index, causing inconsistent RAG results. The production upgrade path (shared Weaviate/Qdrant) is documented in ARCHITECTURE.md §7 as a known limitation.

---

## DDR-005: SQLite Checkpointing over Redis or PostgreSQL

**Decision:** LangGraph's `SqliteSaver` as the checkpoint backend for pipeline state persistence.

**Alternatives considered:**
- Redis: in-memory, fast, supports multiple readers/writers — correct for distributed agent workers, but requires an additional running service for single-instance deployment
- PostgreSQL: durable, shared across instances, supports concurrent access — correct for production multi-instance, but adds schema management complexity and a dependency on a running PG instance during development
- In-memory checkpointing (no persistence): simplest, but crash recovery is impossible — defeats the purpose of checkpointing
- Custom SQLite implementation: unnecessary since LangGraph provides SqliteSaver

**Why this choice:**
SQLite requires no external service. It runs as a file on disk, initialized automatically by LangGraph on first use. For single-instance deployments — which covers development, testing, and the current production scope — SQLite's write performance is more than sufficient for checkpointing. The checkpoint writes are small (one JSON blob per node completion, typically 5-50KB) and infrequent (one write per agent node, 7 writes total per pipeline execution).

The critical property is that `thread_id = incident_id` creates a recoverable checkpoint stream per incident. If the process crashes after `trace_analyzer` completes but before `log_correlator` finishes, the next invocation with the same `incident_id` loads the checkpoint and resumes from `log_correlator` — the 20-second parallel analysis phase is not repeated.

**When this decision reverses:**
When multiple agent service instances are deployed to handle concurrent incidents beyond single-instance Gemini API quota, SQLite breaks immediately. Multiple processes cannot safely write to the same SQLite file concurrently (SQLite supports concurrent readers but serializes writers, and cross-process access has locking edge cases). The migration: swap `SqliteSaver` for `PostgresSaver` in `build_pipeline()` — a one-line change. LangGraph's checkpointer abstraction is exactly the right boundary for this upgrade.

**Consequences:**
SQLite checkpoint files accumulate on disk. A 7-day retention policy cleanup job is needed in production to prevent unbounded growth. The checkpoint DB path is configurable via environment variable (`CHECKPOINT_DB`), allowing tests to use `:memory:` for isolation and production to use a mounted persistent volume.

---

## DDR-006: _dedupe_merge Reducer over operator.add for Shared List Fields

**Decision:** Custom `_dedupe_merge` reducer function on `completed_agents`, `failed_agents`, and `errors` fields in `PostmortemState`.

**Alternatives considered:**
- `operator.add` (list concatenation): the natural LangGraph reducer for append-only lists, but causes `InvalidUpdateError` in the parallel fan-out scenario
- Last-writer-wins (no reducer): loses two of three writes when parallel agents update the same field — silent data loss
- Per-agent fields (e.g., `trace_completed: bool`, `log_completed: bool`): avoids the shared field problem entirely, but requires 14 new fields for the three parallel agents and makes pipeline status reporting more complex
- Deduplication in a post-processing step: correct but adds complexity after the fact rather than at the merge boundary

**Why this choice:**
When trace_analyzer, log_correlator, and metric_reasoner run in parallel via Send(), each agent reads the state at fan-out time. At fan-out, `completed_agents = ["triage"]`. Each agent appends its own name and returns the full list:

```
trace_analyzer returns:   {"completed_agents": ["triage", "trace_analyzer"]}
log_correlator returns:   {"completed_agents": ["triage", "log_correlator"]}
metric_reasoner returns:  {"completed_agents": ["triage", "metric_reasoner"]}
```

With `operator.add`: `["triage", "trace_analyzer"] + ["triage", "log_correlator"] + ["triage", "metric_reasoner"]` = `["triage", "trace_analyzer", "triage", "log_correlator", "triage", "metric_reasoner"]`. "triage" appears three times. LangGraph raises `InvalidUpdateError` because it detects conflicting writes to the same completed state key.

The fix has two parts: each agent returns only its own name (`{"completed_agents": ["trace_analyzer"]}`), and `_dedupe_merge` concatenates without duplicates:

```python
def _dedupe_merge(existing: list, new: list) -> list:
    seen = set(existing)
    result = list(existing)
    for item in new:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result
```

Applied at merge time: `["triage"] ⊕ ["trace_analyzer"] ⊕ ["log_correlator"] ⊕ ["metric_reasoner"]` = `["triage", "trace_analyzer", "log_correlator", "metric_reasoner"]`. Correct, ordered, no duplicates, no InvalidUpdateError.

**Consequences:**
Every agent must return only its own name to `completed_agents`, not the full list. This is enforced by convention (documented in the node contract in DESIGN.md §3) rather than by the type system. A future agent that returns the full list will silently produce correct output due to the deduplication — the reducer is safe to call with full lists — but the convention is worth maintaining for clarity.

---

## DDR-007: run_react_loop Calls llm_with_tools.invoke(messages) Directly

**Decision:** The ReAct loop invokes the LLM with the full message list via `llm_with_tools.invoke(messages)` rather than wrapping tool calls in intermediate format objects.

**Alternatives considered:**
- LangChain's `AgentExecutor`: manages the ReAct loop automatically, but hides the message construction and makes the loop behavior opaque during debugging
- Manual tool dispatch with structured JSON parsing: more control, but requires parsing the LLM's output format manually — fragile across model versions
- LangGraph's built-in tool node: correct for graph-level tool routing, but adds a graph node per tool call — too granular for an agent with multiple sequential tool calls within one node

**Why this choice:**
`llm.bind_tools(tools)` attaches the tool schemas to the LLM client once. The resulting `llm_with_tools` object accepts a plain message list and returns either a final response or a response with `tool_calls` populated. The loop structure is:

```python
response = llm_with_tools.invoke(messages)
if response.tool_calls:
    # execute tools, append ToolMessage results, loop
else:
    return response.content  # done
```

This is the minimal correct implementation. The message list is the source of truth — the full conversation history including tool results is passed on every iteration. This is exactly how the underlying API works (all LLM providers with tool support expect the full conversation context on each call).

The critical benefit for testing: mock LLMs receive the same `invoke(messages)` call signature as real Gemini. A `MagicMock()` that returns a pre-configured response works without any special adapter. The 9 integration tests that use mock LLMs rely on this property.

**Consequences:**
The full message list grows with each tool call iteration. For an agent that makes 7 tool calls, the 8th LLM call sends all prior tool calls and results as context. At Gemini's 1M-token context limit this is not a concern, but for smaller models (Ollama llama3.1:8b with 8K context) a long tool-call chain can exceed the context window. The `max_iterations` parameter in `run_react_loop` caps this at 8 iterations per agent node.

---

## DDR-008: Every Agent Node Catches All Exceptions and Returns Partial State

**Decision:** No agent node propagates exceptions to the LangGraph executor. All exceptions are caught, recorded in `state["errors"]`, and the node returns whatever partial state it produced before the failure.

**Alternatives considered:**
- Let exceptions propagate: LangGraph catches them and marks the node as failed, but the entire pipeline halts — subsequent agents do not run, no postmortem is generated
- Retry at the node level: correct for transient failures (Gemini 429 rate limit), but complex to implement correctly with backoff inside a node without blocking the executor thread
- Sentinel return values: return `None` or empty strings for failed fields, let downstream agents handle None — causes KeyError or silent incorrect behavior

**Why this choice:**
The pipeline serves a specific operational purpose: generate a postmortem report when an incident is detected. A postmortem that says "trace analysis unavailable — Jaeger was unreachable during analysis; root cause determined from metrics and logs alone" is more useful than no postmortem. The operator still gets a structured report with the available information, the confidence score reflects the missing signal, and the errors list explains exactly what failed.

The pattern every agent follows:

```python
def agent_node(state: PostmortemState) -> dict:
    try:
        # ... agent logic ...
        return {
            "agent_findings": result,
            "agent_had_error": True,       # True = success
            "completed_agents": ["agent"], # always written
        }
    except Exception as e:
        return {
            "agent_findings": [],
            "agent_had_error": False,      # False = failed
            "completed_agents": ["agent"], # still mark as visited
            "failed_agents": ["agent"],
            "errors": [f"[agent] {type(e).__name__}: {str(e)[:200]}"],
        }
```

`completed_agents` is written even on failure. This is deliberate: on checkpoint resume, an agent that failed should not re-run — it failed for a reason (Jaeger is down, Gemini quota exhausted) and will fail again. The failed state is durable. `failed_agents` separately tracks which agents had errors, so the correlation agent can adjust its confidence and the postmortem writer can note the data gaps.

**Consequences:**
Agents that fail silently (no exception, but incorrect LLM output — hallucination) are not caught by this mechanism. The exception handler catches infrastructure failures (QueryError, network errors, Gemini API errors) but not semantic failures (LLM invents data). The data quality pre-flight check added to the triage agent (verify tool results contain non-zero data before invoking the LLM) is the partial mitigation for the hallucination case.

---

## DDR-009: Gemini 1.5 Flash as Default Cloud LLM, Ollama as Local Fallback

**Decision:** Auto-select the LLM provider at startup: Gemini 1.5 Flash when `GOOGLE_API_KEY` is set, Ollama (llama3.1:8b) otherwise.

**Alternatives considered:**
- Gemini only: requires API key, excludes evaluators who don't want to create a Google account
- OpenAI GPT-4o: strong reasoning, but not strategically aligned with a Google internship application; adds an OpenAI dependency to a project demonstrating GCP alignment
- Anthropic Claude: similar reasoning to OpenAI — wrong strategic alignment
- Ollama only: no API costs, full privacy, but slower reasoning quality for structured output extraction
- User-configurable only (no auto-selection): correct for production, but increases setup friction for first-time evaluators

**Why this choice:**
The auto-selection serves two distinct use cases with different requirements:

*Development and evaluation:* Anyone who clones the repository should be able to run the full pipeline without creating accounts or adding payment methods. `ollama pull llama3.1:8b` and `docker compose up` is the complete setup. This matters for a portfolio project that needs to be evaluated by engineers with limited time.

*Production and demonstration:* Gemini 1.5 Flash produces more reliable structured output (consistent section headers in postmortem reports, better JSON in tool call arguments) and runs significantly faster (~5-15s/call vs ~15-45s/call on CPU Ollama). For the scenario runs documented in `docs/postmortem-examples/`, Gemini was used.

Both providers implement the same LangChain interface (`ChatGoogleGenerativeAI` and `ChatOllama`). The binding is via `llm.bind_tools(tools)` and `llm.invoke(messages)` — identical call sites in every agent. The abstraction boundary means no agent code changes when switching providers.

*Why temperature=0 for both:* Incident analysis requires deterministic reasoning. The same incident analyzed twice should produce the same root cause hypothesis. Temperature > 0 introduces variance that is useful for creative generation but harmful for diagnostic reasoning where consistent conclusions matter operationally.

**Consequences:**
The quality difference between providers is real and documented. Ollama llama3.1:8b occasionally produces malformed section headers or omits required fields in postmortem output. The regex parsers in each agent node were written defensively to handle formatting variation — they match patterns rather than exact strings, which makes them resilient to model variance. The fallback report template in the postmortem writer provides a structured output even when the LLM produces unusable text.

---

## DDR-010: Parallel Fan-Out to Three Analysis Agents via LangGraph Send() API

**Decision:** Trace analyzer, log correlator, and metric reasoner run concurrently via `Send()` rather than sequentially.

**Alternatives considered:**
- Sequential execution (trace → log → metric → correlation): simplest graph structure, no state merge complexity, but adds ~35 seconds of unnecessary wall-clock time
- Async within a single node (asyncio.gather): possible but mixes async and sync code in the LangGraph executor thread pool — fragile and non-idiomatic
- Separate pipeline runs merged at the end: incorrect — LangGraph pipelines are not designed for external merge
- Three separate gRPC calls from the gateway (parallel invocations): would require the gateway to coordinate partial results — wrong architectural boundary

**Why this choice:**
The three parallel agents query entirely different backends: trace analyzer queries Jaeger, log correlator queries the log store, metric reasoner queries Prometheus. These I/O-bound queries are completely independent — there is no data dependency between them. Sequential execution means the log correlator waits for the trace analyzer to finish a Jaeger query before starting its own Prometheus query. This is wasted time with no correctness benefit.

`Send("trace_analyzer", state)` dispatches a copy of the current state to the trace analyzer node and runs it in a thread from LangGraph's executor pool. The three `Send()` calls return a `List[Send]`, and LangGraph runs all three concurrently. The fan-in is automatic: the correlation node fires only after all three Send targets have completed and merged their results back into the shared state.

Wall-clock time for the parallel phase = max(trace_time, log_time, metric_time) ≈ 20-25 seconds.
Wall-clock time for sequential execution = trace_time + log_time + metric_time ≈ 55 seconds.
Saving: ~30 seconds off the critical path toward the 90-second target.

**Consequences:**
Parallel execution introduced the state merge problem documented in DDR-006. The `_dedupe_merge` reducer is a direct consequence of this decision. The trade-off is explicit: accept the merge complexity to gain the 30-second wall-clock saving. For a system with a 90-second end-to-end target, 30 seconds is not optional overhead — it is the difference between meeting and missing the target.

---

## DDR-011: Single HTML File Dashboard with No Build Step

**Decision:** The web dashboard is a single `index.html` file with inline CSS and JavaScript. No React, no build step, no npm.

**Alternatives considered:**
- React + Vite: component model, better state management, hot reload — but adds `node_modules`, `package.json`, a build step, and a separate development server
- Next.js: server-side rendering, but excessive complexity for a dashboard that polls two endpoints
- Vue.js via CDN: lighter than React, but adds an external CDN dependency that breaks offline use
- Svelte: excellent for small interactive UIs, but unfamiliar to most Go/Python engineers reviewing the project

**Why this choice:**
The dashboard has three interactions: fetch the incident list on load, select an incident to view detail, and receive SSE events to update the reasoning timeline. This is a 200-line JavaScript problem, not a component architecture problem.

A single HTML file means:
- `docker compose up` serves the dashboard with no additional step
- The FastAPI server serves the file directly from `GET /`
- The entire UI is readable in one file — no import chains, no build artifacts
- Offline use works (Ollama path) with no CDN dependencies

The markdown renderer is the most complex piece — a 20-line regex chain that converts the postmortem report to styled HTML. It handles tables, headers, bullet points, code blocks, and bold text. For the postmortem report format that Ghost Debugger generates, this is sufficient.

**Consequences:**
No TypeScript. No component reuse. No hot reload during development (edit the file, refresh the browser). For a production dashboard serving 50 concurrent SREs, React with proper state management would be the right choice. For a portfolio demonstration, a single HTML file that works immediately with `docker compose up` serves the purpose better than a framework with setup friction.

---

## DDR-012: Failure Injector as a Separate Go HTTP Service

**Decision:** Failure injection logic lives in a separate `failure_injector` service with an HTTP control API, rather than being baked into the test services.

**Alternatives considered:**
- Environment variable flags in each service: requires restarting containers to change failure mode — breaks the "inject mid-run" use case
- Chaos Monkey / Chaos Toolkit: production-grade chaos engineering, but heavyweight for a three-service demo environment
- Direct container manipulation (docker pause, tc netem): accurate but requires Docker socket access and root privileges in the container
- In-process failure flags with a control endpoint per service: duplicates the failure logic across three services — three places to maintain the same code

**Why this choice:**
The failure injector needs to change failure state without restarting services — specifically so that traffic can flow, then a failure can be injected mid-run, then traffic can continue while the failure is active, demonstrating the real-time detection capability.

The separation also keeps business logic clean. Each test service's request handler checks `GET /state/{service_name}` on the failure injector and applies whatever configuration is returned. The failure injector maintains state; the test services poll and apply. This is the same control plane / data plane separation that Google's network infrastructure uses: the control plane (failure injector) manages configuration, the data plane (test services) applies it per-request.

The polling interval (5 seconds) means failure injection takes effect within 5 seconds of the API call — acceptable for demo scenarios where the scenario scripts include explicit `sleep` after injection.

**Consequences:**
Services poll the failure injector on every request (with a 5-second debounce cache). If the failure injector is down, services fall back to no-failure mode — fail open, not fail closed. This is the correct behavior: a missing control plane should not cause data plane failures. The health check script (`scripts/preflight_check.sh`) verifies the failure injector is reachable before any scenario runs.

---

*This document is updated when a decision is made, not after implementation is complete.*
*Decisions documented after the fact lose the "alternatives considered" section —*
*the most valuable part, because it shows what was rejected and why.*
