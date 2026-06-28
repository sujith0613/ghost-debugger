import httpx
import logging
import statistics
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Tuple
from agents.storage.base import TimeSeries, DataPoint, QueryError

logger = logging.getLogger(__name__)


class PrometheusClient:
    def __init__(self, base_url: str = "http://localhost:9090"):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=10.0),
        )
        self._anomaly_multiplier = 3.0

    def close(self):
        self._client.close()

    def query_error_rate(
        self,
        service_name: str,
        lookback_minutes: int = 60,
        step_seconds: int = 15,
    ) -> TimeSeries:
        rate_window = "5m"
        promql = (
            f'rate(http_requests_total{{service="{service_name}",'
            f'status=~"5.."}}[{rate_window}])'
            f' / rate(http_requests_total{{service="{service_name}"}}[{rate_window}])'
        )

        return self._range_query(
            promql=promql,
            metric_name="error_rate",
            labels={"service": service_name},
            lookback_minutes=lookback_minutes,
            step_seconds=step_seconds,
        )

    def query_latency_percentile(
        self,
        service_name: str,
        percentile: float = 0.99,
        lookback_minutes: int = 60,
        step_seconds: int = 15,
    ) -> TimeSeries:
        rate_window = "5m"
        promql = (
            f"histogram_quantile({percentile}, "
            f"rate(http_request_duration_seconds_bucket"
            f'{{service="{service_name}"}}[{rate_window}]))'
        )

        return self._range_query(
            promql=promql,
            metric_name=f"latency_p{int(percentile * 100)}",
            labels={"service": service_name, "percentile": str(percentile)},
            lookback_minutes=lookback_minutes,
            step_seconds=step_seconds,
        )

    def query_request_rate(
        self,
        service_name: str,
        lookback_minutes: int = 60,
        step_seconds: int = 15,
    ) -> TimeSeries:
        promql = (
            f'rate(http_requests_total{{service="{service_name}"}}[5m])'
        )
        return self._range_query(
            promql=promql,
            metric_name="request_rate",
            labels={"service": service_name},
            lookback_minutes=lookback_minutes,
            step_seconds=step_seconds,
        )

    def query_gauge(
        self,
        metric_name: str,
        service_name: str,
        lookback_minutes: int = 60,
        step_seconds: int = 15,
        extra_labels: Optional[Dict[str, str]] = None,
    ) -> TimeSeries:
        label_filters = f'service="{service_name}"'
        if extra_labels:
            for k, v in extra_labels.items():
                label_filters += f', {k}="{v}"'

        promql = f"{metric_name}{{{label_filters}}}"

        return self._range_query(
            promql=promql,
            metric_name=metric_name,
            labels={"service": service_name, **(extra_labels or {})},
            lookback_minutes=lookback_minutes,
            step_seconds=step_seconds,
        )

    def query_instant(
        self,
        promql: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Optional[float]:
        try:
            response = self._client.get(
                "/api/v1/query",
                params={"query": promql},
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                return None

            results = data.get("data", {}).get("result", [])
            if not results:
                return None

            value_str = results[0].get("value", [None, None])[1]
            return float(value_str) if value_str else None

        except httpx.HTTPError as e:
            raise QueryError("prometheus", f"instant:{promql[:50]}", e) from e
        except QueryError:
            raise
        except Exception as e:
            raise QueryError("prometheus", f"instant:{promql[:50]}", e) from e

    def get_metric_names(self) -> List[str]:
        try:
            response = self._client.get("/api/v1/label/__name__/values")
            response.raise_for_status()
            data = response.json()
            return sorted(data.get("data", []))
        except Exception as e:
            raise QueryError("prometheus", "GET /api/v1/label/__name__/values", e) from e

    def check_health(self) -> bool:
        try:
            response = self._client.get("/-/healthy", timeout=3.0)
            return response.status_code == 200
        except Exception:
            return False

    def _range_query(
        self,
        promql: str,
        metric_name: str,
        labels: Dict[str, str],
        lookback_minutes: int,
        step_seconds: int,
    ) -> TimeSeries:
        end_time = datetime.now(tz=timezone.utc)
        start_time = end_time - timedelta(minutes=lookback_minutes)

        params = {
            "query": promql,
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
            "step": f"{step_seconds}s",
        }

        try:
            response = self._client.get("/api/v1/query_range", params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                error_type = data.get("errorType", "unknown")
                error_msg = data.get("error", "unknown error")
                raise QueryError(
                    "prometheus",
                    promql[:80],
                    Exception(f"{error_type}: {error_msg}"),
                )

            results = data.get("data", {}).get("result", [])

            if not results:
                return self._empty_series(metric_name, labels)

            raw_series = results[0]
            raw_values = raw_series.get("values", [])

            data_points = []
            for ts, val_str in raw_values:
                try:
                    val = float(val_str)
                    if val == val:
                        data_points.append(DataPoint(timestamp=float(ts), value=val))
                except (ValueError, TypeError):
                    continue

            values = [dp["value"] for dp in data_points]
            min_val = min(values) if values else 0.0
            max_val = max(values) if values else 0.0
            avg_val = statistics.mean(values) if values else 0.0
            latest_val = values[-1] if values else 0.0

            is_anomalous = False
            anomaly_reason = ""
            if avg_val > 0 and latest_val > avg_val * self._anomaly_multiplier:
                is_anomalous = True
                anomaly_reason = (
                    f"latest value ({latest_val:.4f}) is "
                    f"{latest_val / avg_val:.1f}x the average ({avg_val:.4f}) "
                    f"over the last {lookback_minutes} minutes"
                )

            logger.debug(
                "prometheus range query complete",
                extra={
                    "metric": metric_name,
                    "labels": labels,
                    "data_points": len(data_points),
                    "latest": latest_val,
                    "is_anomalous": is_anomalous,
                }
            )

            return TimeSeries(
                metric_name=metric_name,
                labels=labels,
                data_points=data_points,
                min_value=round(min_val, 6),
                max_value=round(max_val, 6),
                avg_value=round(avg_val, 6),
                latest_value=round(latest_val, 6),
                is_anomalous=is_anomalous,
                anomaly_reason=anomaly_reason,
            )

        except httpx.HTTPError as e:
            raise QueryError("prometheus", promql[:80], e) from e
        except QueryError:
            raise
        except Exception as e:
            raise QueryError("prometheus", promql[:80], e) from e

    def _empty_series(self, metric_name: str, labels: Dict[str, str]) -> TimeSeries:
        return TimeSeries(
            metric_name=metric_name,
            labels=labels,
            data_points=[],
            min_value=0.0,
            max_value=0.0,
            avg_value=0.0,
            latest_value=0.0,
            is_anomalous=False,
            anomaly_reason="",
        )
