import logging
from typing import List, Optional, Dict
from datetime import datetime, timezone

from agents.storage.base import (
    Trace, TimeSeries, LogEntry, PastIncident, QueryError
)
from agents.storage.jaeger_client import JaegerClient
from agents.storage.prometheus_client import PrometheusClient
from agents.storage.chromadb_client import ChromaDBClient

logger = logging.getLogger(__name__)


class TelemetryQuerier:
    def __init__(
        self,
        jaeger_url: str = "http://localhost:16686",
        prometheus_url: str = "http://localhost:9090",
        chromadb_host: str = "localhost",
        chromadb_port: int = 8000,
        seed_chromadb: bool = True,
    ):
        self._jaeger = JaegerClient(base_url=jaeger_url)
        self._prometheus = PrometheusClient(base_url=prometheus_url)
        self._chromadb = ChromaDBClient(host=chromadb_host, port=chromadb_port)

        if seed_chromadb:
            self._chromadb.seed_sample_incidents()

        logger.info("TelemetryQuerier initialized",
                    extra={
                        "jaeger_url": jaeger_url,
                        "prometheus_url": prometheus_url,
                        "chromadb_host": chromadb_host,
                        "chromadb_port": chromadb_port,
                        "chromadb_docs": self._chromadb.collection_size(),
                    })

    def close(self):
        self._jaeger.close()
        self._prometheus.close()

    def query_traces(
        self,
        service_name: str,
        lookback_minutes: int = 60,
        limit: int = 100,
        only_errors: bool = False,
    ) -> List[Trace]:
        return self._jaeger.query_traces(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
            limit=limit,
            only_errors=only_errors,
        )

    def get_trace(self, trace_id: str) -> Optional[Trace]:
        return self._jaeger.get_trace(trace_id)

    def compute_trace_error_rate(
        self,
        service_name: str,
        lookback_minutes: int = 60,
    ) -> Dict[str, float]:
        return self._jaeger.compute_error_rate(service_name, lookback_minutes)

    def get_services(self) -> List[str]:
        return self._jaeger.get_services()

    def query_error_rate(
        self,
        service_name: str,
        lookback_minutes: int = 60,
    ) -> TimeSeries:
        return self._prometheus.query_error_rate(service_name, lookback_minutes)

    def query_latency_p99(
        self,
        service_name: str,
        lookback_minutes: int = 60,
    ) -> TimeSeries:
        return self._prometheus.query_latency_percentile(
            service_name, percentile=0.99, lookback_minutes=lookback_minutes
        )

    def query_latency_p50(
        self,
        service_name: str,
        lookback_minutes: int = 60,
    ) -> TimeSeries:
        return self._prometheus.query_latency_percentile(
            service_name, percentile=0.50, lookback_minutes=lookback_minutes
        )

    def query_request_rate(
        self,
        service_name: str,
        lookback_minutes: int = 60,
    ) -> TimeSeries:
        return self._prometheus.query_request_rate(service_name, lookback_minutes)

    def query_db_connections(self, service_name: str, lookback_minutes: int = 60) -> TimeSeries:
        return self._prometheus.query_gauge(
            metric_name="db_connections_active",
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def query_memory_usage(self, service_name: str, lookback_minutes: int = 60) -> TimeSeries:
        return self._prometheus.query_gauge(
            metric_name="memory_bytes",
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def query_goroutine_count(self, service_name: str, lookback_minutes: int = 60) -> TimeSeries:
        return self._prometheus.query_gauge(
            metric_name="goroutines_count",
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def query_logs(
        self,
        service_name: str,
        level: str = "ERROR",
        lookback_minutes: int = 60,
        limit: int = 100,
    ) -> List[LogEntry]:
        try:
            traces = self._jaeger.query_traces(
                service_name=service_name,
                lookback_minutes=lookback_minutes,
                limit=limit,
                only_errors=(level == "ERROR"),
            )

            log_entries: List[LogEntry] = []
            for trace in traces:
                for span in trace["spans"]:
                    if span["service_name"] != service_name:
                        continue
                    if level == "ERROR" and not span["is_error"]:
                        continue

                    if span["error_message"] or span["is_error"]:
                        log_entry = LogEntry(
                            log_id=f"{span['span_id']}-log",
                            service_name=span["service_name"],
                            level="ERROR" if span["is_error"] else level,
                            message=span["error_message"] or span["operation_name"],
                            trace_id=trace["trace_id"],
                            span_id=span["span_id"],
                            timestamp=datetime.fromtimestamp(
                                span["start_time_us"] / 1_000_000,
                                tz=timezone.utc
                            ).isoformat(),
                            fields={
                                "operation": span["operation_name"],
                                "duration_us": str(span["duration_us"]),
                                **span["attributes"],
                            },
                        )
                        log_entries.append(log_entry)

            return log_entries[:limit]

        except QueryError:
            raise
        except Exception as e:
            raise QueryError("jaeger", f"query_logs:{service_name}", e) from e

    def search_similar_incidents(
        self,
        query_text: str,
        n_results: int = 3,
        min_similarity: float = 0.3,
        exclude_incident_id: Optional[str] = None,
    ) -> List[PastIncident]:
        return self._chromadb.search_similar_incidents(
            query_text=query_text,
            n_results=n_results,
            min_similarity=min_similarity,
            exclude_incident_id=exclude_incident_id,
        )

    def store_postmortem(self, **kwargs) -> str:
        return self._chromadb.store_postmortem(**kwargs)

    def check_backends(self) -> Dict[str, bool]:
        health = {}

        try:
            services = self._jaeger.get_services()
            health["jaeger"] = True
            logger.info("Jaeger healthy", extra={"services": services})
        except Exception as e:
            health["jaeger"] = False
            logger.warning(f"Jaeger unhealthy: {e}")

        try:
            health["prometheus"] = self._prometheus.check_health()
            if health["prometheus"]:
                logger.info("Prometheus healthy")
            else:
                logger.warning("Prometheus returned unhealthy status")
        except Exception as e:
            health["prometheus"] = False
            logger.warning(f"Prometheus unhealthy: {e}")

        try:
            size = self._chromadb.collection_size()
            health["chromadb"] = size >= 0
            logger.info("ChromaDB healthy", extra={"collection_size": size})
        except Exception as e:
            health["chromadb"] = False
            logger.warning(f"ChromaDB unhealthy: {e}")

        return health
