import chromadb
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from agents.storage.base import PastIncident, QueryError

logger = logging.getLogger(__name__)

POSTMORTEM_COLLECTION = "ghost_debugger_postmortems"
DEFAULT_N_RESULTS = 3


class ChromaDBClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8000,
        collection_name: str = POSTMORTEM_COLLECTION,
    ):
        self.collection_name = collection_name

        try:
            self._chroma = chromadb.HttpClient(
                host=host,
                port=port,
                settings=chromadb.Settings(
                    anonymized_telemetry=False,
                ),
            )

            self._collection = self._chroma.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            logger.info(
                "ChromaDB client initialized",
                extra={
                    "host": host,
                    "port": port,
                    "collection": collection_name,
                    "existing_docs": self._collection.count(),
                }
            )

        except Exception as e:
            raise QueryError("chromadb", "initialization", e) from e

    def store_postmortem(
        self,
        incident_id: str,
        title: str,
        root_cause: str,
        affected_services: List[str],
        severity: str,
        occurred_at: str,
        resolved_at: str,
        time_to_resolve_minutes: int,
        postmortem_summary: str,
        action_items: List[str],
        postmortem_full_markdown: str,
    ) -> str:
        document_text = f"{title}\n\nRoot Cause: {root_cause}\n\n{postmortem_summary}"

        metadata = {
            "title": title,
            "root_cause": root_cause,
            "affected_services": ",".join(affected_services),
            "severity": severity,
            "occurred_at": occurred_at,
            "resolved_at": resolved_at,
            "time_to_resolve_minutes": time_to_resolve_minutes,
            "action_items": "\n".join(action_items),
            "postmortem_summary": postmortem_summary,
            "postmortem_markdown_preview": postmortem_full_markdown[:4000],
            "stored_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        try:
            self._collection.upsert(
                ids=[incident_id],
                documents=[document_text],
                metadatas=[metadata],
            )

            logger.info(
                "postmortem stored in ChromaDB",
                extra={
                    "incident_id": incident_id,
                    "severity": severity,
                    "affected_services": affected_services,
                    "collection_size": self._collection.count(),
                }
            )

            return incident_id

        except Exception as e:
            raise QueryError("chromadb", f"store_postmortem:{incident_id}", e) from e

    def search_similar_incidents(
        self,
        query_text: str,
        n_results: int = DEFAULT_N_RESULTS,
        min_similarity: float = 0.3,
        exclude_incident_id: Optional[str] = None,
    ) -> List[PastIncident]:
        collection_size = self._collection.count()

        if collection_size == 0:
            logger.debug("ChromaDB collection is empty — no similar incidents to find")
            return []

        actual_n = min(n_results + 1, collection_size)

        try:
            results = self._collection.query(
                query_texts=[query_text],
                n_results=actual_n,
                include=["documents", "metadatas", "distances"],
            )

            incidents = []

            ids = results.get("ids", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            for doc_id, metadata, distance in zip(ids, metadatas, distances):
                if exclude_incident_id and doc_id == exclude_incident_id:
                    continue

                similarity = 1.0 - (distance / 2.0)

                if similarity < min_similarity:
                    continue

                services_str = metadata.get("affected_services", "")
                affected_services = [s.strip() for s in services_str.split(",") if s.strip()]

                action_items_str = metadata.get("action_items", "")
                action_items = [a.strip() for a in action_items_str.split("\n") if a.strip()]

                incidents.append(PastIncident(
                    incident_id=doc_id,
                    title=metadata.get("title", ""),
                    root_cause=metadata.get("root_cause", ""),
                    affected_services=affected_services,
                    severity=metadata.get("severity", "UNKNOWN"),
                    occurred_at=metadata.get("occurred_at", ""),
                    resolved_at=metadata.get("resolved_at", ""),
                    time_to_resolve_minutes=int(metadata.get("time_to_resolve_minutes", 0)),
                    postmortem_summary=metadata.get("postmortem_summary", ""),
                    action_items=action_items,
                    similarity_score=round(similarity, 4),
                ))

            incidents.sort(key=lambda x: x["similarity_score"], reverse=True)

            logger.debug(
                "ChromaDB similarity search complete",
                extra={
                    "query_length": len(query_text),
                    "results_returned": len(incidents),
                    "top_similarity": incidents[0]["similarity_score"] if incidents else 0.0,
                }
            )

            return incidents[:n_results]

        except Exception as e:
            raise QueryError("chromadb", f"search_similar:{query_text[:50]}", e) from e

    def get_incident(self, incident_id: str) -> Optional[PastIncident]:
        try:
            results = self._collection.get(
                ids=[incident_id],
                include=["documents", "metadatas"],
            )

            ids = results.get("ids", [])
            if not ids:
                return None

            metadata = results.get("metadatas", [{}])[0]

            services_str = metadata.get("affected_services", "")
            affected_services = [s.strip() for s in services_str.split(",") if s.strip()]

            action_items_str = metadata.get("action_items", "")
            action_items = [a.strip() for a in action_items_str.split("\n") if a.strip()]

            return PastIncident(
                incident_id=ids[0],
                title=metadata.get("title", ""),
                root_cause=metadata.get("root_cause", ""),
                affected_services=affected_services,
                severity=metadata.get("severity", "UNKNOWN"),
                occurred_at=metadata.get("occurred_at", ""),
                resolved_at=metadata.get("resolved_at", ""),
                time_to_resolve_minutes=int(metadata.get("time_to_resolve_minutes", 0)),
                postmortem_summary=metadata.get("postmortem_summary", ""),
                action_items=action_items,
                similarity_score=1.0,
            )

        except Exception as e:
            raise QueryError("chromadb", f"get_incident:{incident_id}", e) from e

    def collection_size(self) -> int:
        try:
            return self._collection.count()
        except Exception:
            return -1

    def seed_sample_incidents(self):
        if self._collection.count() > 0:
            logger.debug("ChromaDB collection already has data — skipping seed")
            return

        sample_incidents = [
            {
                "incident_id": "INC-2024-11-03",
                "title": "service_b database connection pool exhaustion",
                "root_cause": (
                    "Database connection pool size (100) was insufficient for the "
                    "traffic spike during peak hours. Long-running transactions held "
                    "connections longer than expected, causing pool exhaustion and "
                    "cascading failures to service_a."
                ),
                "affected_services": ["service_a", "service_b"],
                "severity": "SEV1",
                "occurred_at": "2024-11-03T14:03:00Z",
                "resolved_at": "2024-11-03T14:50:00Z",
                "time_to_resolve_minutes": 47,
                "postmortem_summary": (
                    "A traffic spike at 14:03 UTC caused service_b's database connection "
                    "pool (max=100) to become fully saturated. New requests to service_b "
                    "could not acquire database connections and returned 503 errors. "
                    "service_a, which calls service_b synchronously, began returning 502 "
                    "errors to users. Error rate on service_b reached 31% within 2 minutes. "
                    "Resolution: increased connection pool to 200, added circuit breaker "
                    "on service_a for service_b calls, added query timeout enforcement."
                ),
                "action_items": [
                    "Increase database connection pool size to 200",
                    "Add circuit breaker pattern on service_a -> service_b calls",
                    "Implement query timeout of 5 seconds for all database queries",
                    "Add Prometheus alert for db_connections_active > 80% of pool size",
                    "Implement connection pool monitoring dashboard in Grafana",
                ],
            },
            {
                "incident_id": "INC-2024-09-15",
                "title": "service_a memory leak causing OOM and service restart",
                "root_cause": (
                    "A memory leak in service_a's request handler caused gradual "
                    "memory accumulation over 6 hours. The Go runtime's garbage collector "
                    "could not reclaim the leaked memory (held in a global cache). "
                    "service_a was OOM-killed by the container runtime at 09:15 UTC."
                ),
                "affected_services": ["service_a"],
                "severity": "SEV2",
                "occurred_at": "2024-09-15T09:15:00Z",
                "resolved_at": "2024-09-15T09:45:00Z",
                "time_to_resolve_minutes": 30,
                "postmortem_summary": (
                    "service_a experienced a gradual memory increase starting at 03:00 UTC. "
                    "By 09:15 UTC, memory usage reached the container limit (512MB) and "
                    "the container was OOM-killed. The service restarted automatically "
                    "within 30 seconds, but during the restart window, all requests to "
                    "service_a returned 502 errors. Root cause identified as an unbounded "
                    "in-memory cache that accumulated entries without eviction."
                ),
                "action_items": [
                    "Add TTL-based eviction to the in-memory cache",
                    "Add Prometheus alert for memory_bytes > 400MB (80% of limit)",
                    "Implement memory profiling in CI pipeline",
                    "Increase container memory limit to 1GB as short-term mitigation",
                ],
            },
            {
                "incident_id": "INC-2024-07-22",
                "title": "service_c high latency caused by upstream dependency timeout",
                "root_cause": (
                    "An external API that service_c depends on (payment provider) "
                    "began responding slowly (p99 > 10 seconds). service_c had no "
                    "timeout configured for this external call, causing requests to "
                    "hang and goroutines to pile up, eventually exhausting the goroutine pool."
                ),
                "affected_services": ["service_a", "service_b", "service_c"],
                "severity": "SEV1",
                "occurred_at": "2024-07-22T11:30:00Z",
                "resolved_at": "2024-07-22T13:15:00Z",
                "time_to_resolve_minutes": 105,
                "postmortem_summary": (
                    "Starting at 11:30 UTC, the external payment provider API began "
                    "returning responses with p99 latency above 10 seconds. service_c "
                    "had no timeout on external HTTP calls. Goroutines accumulated waiting "
                    "for the external API, exhausting the goroutine pool. service_b, which "
                    "calls service_c, began timing out. service_a, calling service_b, "
                    "began returning 504 errors to users. Full cascade failure took 8 minutes "
                    "to develop from first slow response to full outage."
                ),
                "action_items": [
                    "Add 5-second timeout to all external HTTP client calls",
                    "Implement circuit breaker for external payment API",
                    "Add goroutine count to Prometheus monitoring",
                    "Add Prometheus alert for goroutine_count > 10000",
                    "Implement fallback behavior when payment API is unavailable",
                ],
            },
        ]

        for inc in sample_incidents:
            try:
                self.store_postmortem(
                    incident_id=inc["incident_id"],
                    title=inc["title"],
                    root_cause=inc["root_cause"],
                    affected_services=inc["affected_services"],
                    severity=inc["severity"],
                    occurred_at=inc["occurred_at"],
                    resolved_at=inc["resolved_at"],
                    time_to_resolve_minutes=inc["time_to_resolve_minutes"],
                    postmortem_summary=inc["postmortem_summary"],
                    action_items=inc["action_items"],
                    postmortem_full_markdown="",
                )
            except Exception as e:
                logger.warning(f"Failed to seed incident {inc['incident_id']}: {e}")

        logger.info(f"Seeded ChromaDB with {len(sample_incidents)} sample incidents")
