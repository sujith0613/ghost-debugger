import httpx
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from agents.storage.base import Span, Trace, QueryError

logger = logging.getLogger(__name__)


class JaegerClient:
    def __init__(self, base_url: str = "http://localhost:16686"):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=5.0,
                read=30.0,
                write=5.0,
                pool=10.0,
            ),
        )

    def close(self):
        self._client.close()

    def get_services(self) -> List[str]:
        try:
            response = self._client.get("/api/services")
            response.raise_for_status()
            data = response.json()
            return sorted(data.get("data", []))
        except httpx.HTTPError as e:
            raise QueryError("jaeger", "GET /api/services", e) from e
        except Exception as e:
            raise QueryError("jaeger", "GET /api/services", e) from e

    def query_traces(
        self,
        service_name: str,
        lookback_minutes: int = 60,
        limit: int = 100,
        only_errors: bool = False,
        min_duration_us: Optional[int] = None,
    ) -> List[Trace]:
        params = {
            "service": service_name,
            "lookback": f"{lookback_minutes}m",
            "limit": limit,
        }

        if only_errors:
            params["tags"] = '{"error":"true"}'
        if min_duration_us is not None:
            params["minDuration"] = f"{min_duration_us}us"

        try:
            response = self._client.get("/api/traces", params=params)
            response.raise_for_status()
            data = response.json()

            raw_traces = data.get("data", [])
            traces = [self._parse_trace(raw) for raw in raw_traces]

            logger.debug(
                "jaeger trace query complete",
                extra={
                    "service": service_name,
                    "lookback_minutes": lookback_minutes,
                    "traces_returned": len(traces),
                    "error_traces": sum(1 for t in traces if t["has_error"]),
                }
            )

            return traces
        except httpx.HTTPError as e:
            raise QueryError("jaeger", f"GET /api/traces?service={service_name}", e) from e
        except QueryError:
            raise
        except Exception as e:
            raise QueryError("jaeger", f"GET /api/traces?service={service_name}", e) from e

    def get_trace(self, trace_id: str) -> Optional[Trace]:
        try:
            response = self._client.get(f"/api/traces/{trace_id}")

            if response.status_code == 404:
                return None

            response.raise_for_status()
            data = response.json()
            raw_traces = data.get("data", [])

            if not raw_traces:
                return None

            return self._parse_trace(raw_traces[0])
        except httpx.HTTPError as e:
            raise QueryError("jaeger", f"GET /api/traces/{trace_id}", e) from e
        except QueryError:
            raise
        except Exception as e:
            raise QueryError("jaeger", f"GET /api/traces/{trace_id}", e) from e

    def compute_error_rate(
        self,
        service_name: str,
        lookback_minutes: int = 60,
    ) -> Dict[str, float]:
        try:
            all_traces = self.query_traces(service_name, lookback_minutes, limit=1000)

            if not all_traces:
                return {
                    "total_traces": 0,
                    "error_traces": 0,
                    "error_rate": 0.0,
                    "p50_duration_us": 0,
                    "p99_duration_us": 0,
                }

            total = len(all_traces)
            errors = sum(1 for t in all_traces if t["has_error"])
            durations = sorted(t["duration_us"] for t in all_traces)

            p50_idx = int(total * 0.50)
            p99_idx = int(total * 0.99)

            return {
                "total_traces": total,
                "error_traces": errors,
                "error_rate": round(errors / total, 4) if total > 0 else 0.0,
                "p50_duration_us": durations[p50_idx] if durations else 0,
                "p99_duration_us": durations[min(p99_idx, total - 1)] if durations else 0,
            }
        except QueryError:
            raise
        except Exception as e:
            raise QueryError("jaeger", f"compute_error_rate:{service_name}", e) from e

    def _parse_trace(self, raw: Dict[str, Any]) -> Trace:
        trace_id = raw.get("traceID", "")
        processes = raw.get("processes", {})
        raw_spans = raw.get("spans", [])

        spans = [self._parse_span(s, processes) for s in raw_spans]

        services = list({s["service_name"] for s in spans if s["service_name"]})
        has_error = any(s["is_error"] for s in spans)

        root_span = next(
            (s for s in spans if not s["parent_span_id"]),
            spans[0] if spans else None
        )

        if spans:
            earliest_start = min(s["start_time_us"] for s in spans)
            latest_end = max(
                s["start_time_us"] + s["duration_us"] for s in spans
            )
            total_duration_us = latest_end - earliest_start

            start_dt = datetime.fromtimestamp(
                earliest_start / 1_000_000, tz=timezone.utc
            )
            start_time_iso = start_dt.isoformat()
        else:
            total_duration_us = 0
            start_time_iso = ""

        return Trace(
            trace_id=trace_id,
            spans=spans,
            services=sorted(services),
            duration_us=total_duration_us,
            has_error=has_error,
            root_service=root_span["service_name"] if root_span else "",
            root_operation=root_span["operation_name"] if root_span else "",
            start_time=start_time_iso,
        )

    def _parse_span(
        self, raw: Dict[str, Any], processes: Dict[str, Any]
    ) -> Span:
        process_id = raw.get("processID", "")
        process = processes.get(process_id, {})
        service_name = process.get("serviceName", "unknown")

        parent_span_id = ""
        for ref in raw.get("references", []):
            if ref.get("refType") == "CHILD_OF":
                parent_span_id = ref.get("spanID", "")
                break

        attributes: Dict[str, str] = {}
        is_error = False
        for tag in raw.get("tags", []):
            key = tag.get("key", "")
            value = str(tag.get("value", ""))
            attributes[key] = value
            if key == "error" and value.lower() == "true":
                is_error = True

        error_message = ""
        for log_entry in raw.get("logs", []):
            for field in log_entry.get("fields", []):
                if field.get("key") in ("message", "error", "event"):
                    error_message = str(field.get("value", ""))
                    break

        return Span(
            span_id=raw.get("spanID", ""),
            parent_span_id=parent_span_id,
            operation_name=raw.get("operationName", ""),
            service_name=service_name,
            start_time_us=raw.get("startTime", 0),
            duration_us=raw.get("duration", 0),
            is_error=is_error,
            error_message=error_message,
            attributes=attributes,
        )
