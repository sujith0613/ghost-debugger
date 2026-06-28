import pytest
from unittest.mock import patch, MagicMock
import httpx
from agents.storage.jaeger_client import JaegerClient
from agents.storage.base import QueryError


def make_jaeger_trace(
    trace_id="abc123def456abc1",
    span_id="span001",
    service_name="service_a",
    operation="HTTP GET /api/process",
    duration_us=52000,
    is_error=False,
):
    tags = []
    if is_error:
        tags.append({"key": "error", "value": "true", "type": "string"})

    return {
        "traceID": trace_id,
        "spans": [{
            "traceID": trace_id,
            "spanID": span_id,
            "operationName": operation,
            "references": [],
            "startTime": 1733500000000000,
            "duration": duration_us,
            "tags": tags,
            "logs": [],
            "processID": "p1",
        }],
        "processes": {
            "p1": {
                "serviceName": service_name,
                "tags": [],
            }
        },
    }


class TestJaegerClientParsing:
    def setup_method(self):
        self.client = JaegerClient(base_url="http://localhost:16686")

    def teardown_method(self):
        self.client.close()

    def test_parse_trace_basic_fields(self):
        raw = make_jaeger_trace(
            trace_id="abc123",
            service_name="service_b",
            duration_us=100_000,
        )
        trace = self.client._parse_trace(raw)

        assert trace["trace_id"] == "abc123"
        assert "service_b" in trace["services"]
        assert trace["root_service"] == "service_b"
        assert trace["has_error"] is False
        assert trace["duration_us"] == 100_000

    def test_parse_trace_error_detection(self):
        raw = make_jaeger_trace(is_error=True)
        trace = self.client._parse_trace(raw)
        assert trace["has_error"] is True

    def test_parse_trace_multi_service(self):
        raw = {
            "traceID": "trace-multi",
            "spans": [
                {
                    "traceID": "trace-multi",
                    "spanID": "span-a",
                    "operationName": "service_a.process",
                    "references": [],
                    "startTime": 1733500000000000,
                    "duration": 100_000,
                    "tags": [],
                    "logs": [],
                    "processID": "p1",
                },
                {
                    "traceID": "trace-multi",
                    "spanID": "span-b",
                    "operationName": "service_b.process",
                    "references": [{"refType": "CHILD_OF", "spanID": "span-a"}],
                    "startTime": 1733500000010000,
                    "duration": 80_000,
                    "tags": [],
                    "logs": [],
                    "processID": "p2",
                },
            ],
            "processes": {
                "p1": {"serviceName": "service_a", "tags": []},
                "p2": {"serviceName": "service_b", "tags": []},
            },
        }

        trace = self.client._parse_trace(raw)

        assert "service_a" in trace["services"]
        assert "service_b" in trace["services"]
        assert len(trace["services"]) == 2
        assert trace["root_service"] == "service_a"

        root_span = next(s for s in trace["spans"] if s["span_id"] == "span-a")
        child_span = next(s for s in trace["spans"] if s["span_id"] == "span-b")

        assert root_span["parent_span_id"] == ""
        assert child_span["parent_span_id"] == "span-a"

    def test_parse_span_attributes(self):
        raw_trace = {
            "traceID": "t1",
            "spans": [{
                "traceID": "t1",
                "spanID": "s1",
                "operationName": "db.query",
                "references": [],
                "startTime": 1733500000000000,
                "duration": 15_000,
                "tags": [
                    {"key": "db.system", "value": "postgresql"},
                    {"key": "db.operation", "value": "SELECT"},
                    {"key": "error", "value": "true"},
                ],
                "logs": [{"fields": [{"key": "message", "value": "connection refused"}]}],
                "processID": "p1",
            }],
            "processes": {"p1": {"serviceName": "service_b", "tags": []}},
        }

        trace = self.client._parse_trace(raw_trace)
        span = trace["spans"][0]

        assert span["attributes"]["db.system"] == "postgresql"
        assert span["attributes"]["db.operation"] == "SELECT"
        assert span["is_error"] is True
        assert span["error_message"] == "connection refused"

    def test_parse_empty_trace(self):
        raw = {"traceID": "empty", "spans": [], "processes": {}}
        trace = self.client._parse_trace(raw)

        assert trace["trace_id"] == "empty"
        assert trace["spans"] == []
        assert trace["has_error"] is False
        assert trace["duration_us"] == 0


class TestJaegerClientHTTP:
    def setup_method(self):
        self.client = JaegerClient(base_url="http://localhost:16686")

    def teardown_method(self):
        self.client.close()

    def test_query_traces_success(self):
        raw_trace = make_jaeger_trace(service_name="service_a")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [raw_trace]}
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            traces = self.client.query_traces("service_a", lookback_minutes=30)

        assert len(traces) == 1
        assert traces[0]["root_service"] == "service_a"

    def test_query_traces_empty_result(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            traces = self.client.query_traces("service_a")

        assert traces == []

    def test_query_traces_network_error(self):
        with patch.object(self.client._client, "get",
                          side_effect=httpx.ConnectError("connection refused")):
            with pytest.raises(QueryError) as exc_info:
                self.client.query_traces("service_a")

        assert exc_info.value.backend == "jaeger"

    def test_get_services_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": ["service_a", "service_b", "service_c"]
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            services = self.client.get_services()

        assert services == ["service_a", "service_b", "service_c"]

    def test_compute_error_rate(self):
        traces = [
            make_jaeger_trace(is_error=True),
            make_jaeger_trace(is_error=True),
            make_jaeger_trace(is_error=False),
            make_jaeger_trace(is_error=False),
            make_jaeger_trace(is_error=False),
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": traces}
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            stats = self.client.compute_error_rate("service_b", lookback_minutes=30)

        assert stats["total_traces"] == 5
        assert stats["error_traces"] == 2
        assert abs(stats["error_rate"] - 0.4) < 0.001
