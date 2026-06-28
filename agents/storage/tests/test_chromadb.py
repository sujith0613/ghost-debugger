import pytest
from unittest.mock import patch
import chromadb
from agents.storage.chromadb_client import ChromaDBClient, POSTMORTEM_COLLECTION
from agents.storage.base import QueryError


@pytest.fixture
def in_memory_client():
    ephemeral = chromadb.EphemeralClient()
    with patch("agents.storage.chromadb_client.chromadb.HttpClient",
               return_value=ephemeral):
        client = ChromaDBClient(host="localhost", port=8000)
        try:
            ephemeral.delete_collection(POSTMORTEM_COLLECTION)
        except ValueError:
            pass
        client._collection = ephemeral.get_or_create_collection(
            POSTMORTEM_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        yield client


def make_incident(**overrides):
    base = {
        "incident_id": "INC-TEST-001",
        "title": "service_b database connection pool exhaustion",
        "root_cause": "Database connection pool exhausted during traffic spike",
        "affected_services": ["service_a", "service_b"],
        "severity": "SEV1",
        "occurred_at": "2024-11-03T14:03:00Z",
        "resolved_at": "2024-11-03T14:50:00Z",
        "time_to_resolve_minutes": 47,
        "postmortem_summary": (
            "service_b's database connection pool became exhausted during a traffic spike. "
            "service_a began returning 502 errors. Root cause: connection pool too small."
        ),
        "action_items": ["Increase pool size", "Add circuit breaker"],
        "postmortem_full_markdown": "# Postmortem\n\nFull markdown here.",
    }
    base.update(overrides)
    return base


class TestChromaDBClientStore:
    def test_store_postmortem_returns_incident_id(self, in_memory_client):
        incident = make_incident()
        result = in_memory_client.store_postmortem(**incident)
        assert result == incident["incident_id"]

    def test_store_increments_collection_size(self, in_memory_client):
        assert in_memory_client.collection_size() == 0

        in_memory_client.store_postmortem(**make_incident(incident_id="INC-001"))
        assert in_memory_client.collection_size() == 1

        in_memory_client.store_postmortem(**make_incident(incident_id="INC-002"))
        assert in_memory_client.collection_size() == 2

    def test_upsert_does_not_duplicate(self, in_memory_client):
        incident = make_incident()
        in_memory_client.store_postmortem(**incident)
        in_memory_client.store_postmortem(**incident)
        assert in_memory_client.collection_size() == 1


class TestChromaDBClientRetrieval:
    def test_get_incident_by_id(self, in_memory_client):
        incident = make_incident()
        in_memory_client.store_postmortem(**incident)

        retrieved = in_memory_client.get_incident(incident["incident_id"])

        assert retrieved is not None
        assert retrieved["incident_id"] == incident["incident_id"]
        assert retrieved["root_cause"] == incident["root_cause"]
        assert "service_a" in retrieved["affected_services"]
        assert "service_b" in retrieved["affected_services"]
        assert retrieved["severity"] == "SEV1"

    def test_get_nonexistent_incident_returns_none(self, in_memory_client):
        result = in_memory_client.get_incident("INC-DOES-NOT-EXIST")
        assert result is None


class TestChromaDBClientSearch:
    def test_empty_collection_returns_empty_list(self, in_memory_client):
        results = in_memory_client.search_similar_incidents(
            "database connection pool exhausted"
        )
        assert results == []

    def test_similar_incidents_returned(self, in_memory_client):
        in_memory_client.store_postmortem(**make_incident(
            incident_id="INC-DB-001",
            title="database connection pool exhaustion",
            root_cause="DB pool exhausted",
            postmortem_summary="Database connections ran out during peak load.",
        ))
        in_memory_client.store_postmortem(**make_incident(
            incident_id="INC-MEM-001",
            title="memory leak in service_a",
            root_cause="Unbounded cache caused OOM",
            postmortem_summary="Memory grew unboundedly until OOM kill.",
        ))

        results = in_memory_client.search_similar_incidents(
            "database connection pool is exhausted, service_b is returning errors",
            n_results=2,
            min_similarity=0.0,
        )

        assert len(results) >= 1
        incident_ids = [r["incident_id"] for r in results]
        assert "INC-DB-001" in incident_ids

    def test_results_sorted_by_similarity(self, in_memory_client):
        for i in range(3):
            in_memory_client.store_postmortem(**make_incident(
                incident_id=f"INC-{i:03d}",
                title=f"incident {i}",
            ))

        results = in_memory_client.search_similar_incidents(
            "some query",
            n_results=3,
            min_similarity=0.0,
        )

        if len(results) >= 2:
            for i in range(len(results) - 1):
                assert results[i]["similarity_score"] >= results[i + 1]["similarity_score"]

    def test_exclude_incident_id(self, in_memory_client):
        incident_id = "INC-CURRENT"
        in_memory_client.store_postmortem(**make_incident(incident_id=incident_id))
        in_memory_client.store_postmortem(**make_incident(incident_id="INC-OTHER"))

        results = in_memory_client.search_similar_incidents(
            "database connection pool exhausted",
            min_similarity=0.0,
            exclude_incident_id=incident_id,
        )

        result_ids = [r["incident_id"] for r in results]
        assert incident_id not in result_ids


class TestChromaDBClientSeed:
    def test_seed_populates_collection(self, in_memory_client):
        in_memory_client.seed_sample_incidents()
        assert in_memory_client.collection_size() == 3

    def test_seed_is_idempotent(self, in_memory_client):
        in_memory_client.seed_sample_incidents()
        in_memory_client.seed_sample_incidents()
        assert in_memory_client.collection_size() == 3
