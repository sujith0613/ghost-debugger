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
- All Python: FastAPI could handle ingestion, but Go's concurrency model 
  (goroutines, channels) is significantly better for high-throughput I/O

**Why this choice:**
Go's goroutine-per-request model handles thousands of concurrent telemetry 
ingestion calls with minimal overhead. Python's GIL would be a bottleneck 
at the ingestion layer. LangGraph is Python-native — fighting this by using 
Go for agents would cost development velocity for no gain.

**Consequences:**
Cross-language communication requires gRPC. Proto files become the shared 
API contract between the two languages. Code generation step added to build process.

---

## DDR-002: gRPC over REST for inter-service communication

**Decision:** All service-to-service communication uses gRPC + protobuf.

**Alternatives considered:**
- REST/JSON: simpler to debug (curl-able), but 5x larger payloads, slower parsing
- GraphQL: unnecessary complexity for this use case
- MessagePack over HTTP: binary but no streaming, no generated clients

**Why this choice:**
[Fill this in after reading Section 6.1 of ARCHITECTURE.md]

**Consequences:**
[Fill this in after implementation reveals real tradeoffs]

---

## DDR-003: Token bucket over leaky bucket for rate limiting

[Fill in during implementation of ratelimiter package]

---

## DDR-004: ChromaDB over Pinecone/Weaviate

[Fill in during implementation of RAG layer]

---

## DDR-005: SQLite checkpointing over Redis

[Fill in during LangGraph implementation]

---

*Add new DDR entries as decisions are made during implementation.*
*The act of writing this forces the decision to be conscious, not accidental.*
