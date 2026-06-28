import pytest
from unittest.mock import patch, MagicMock
import httpx
from agents.storage.prometheus_client import PrometheusClient
from agents.storage.base import QueryError


def make_prom_response(values: list, metric_labels: dict = None) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{
                "metric": metric_labels or {"service": "service_b"},
                "values": values,
            }]
        }
    }


class TestPrometheusClientParsing:
    def setup_method(self):
        self.client = PrometheusClient(base_url="http://localhost:9090")

    def teardown_method(self):
        self.client.close()

    def test_empty_response_returns_empty_series(self):
        empty_resp = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = empty_resp
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            ts = self.client.query_error_rate("service_a", lookback_minutes=30)

        assert ts["data_points"] == []
        assert ts["latest_value"] == 0.0
        assert ts["is_anomalous"] is False

    def test_statistics_computed_correctly(self):
        values = [
            [1733500000, "0.02"],
            [1733500015, "0.03"],
            [1733500030, "0.02"],
            [1733500045, "0.04"],
            [1733500060, "0.03"],
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = make_prom_response(values)
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            ts = self.client.query_error_rate("service_b", lookback_minutes=5)

        assert ts["min_value"] == pytest.approx(0.02, abs=1e-4)
        assert ts["max_value"] == pytest.approx(0.04, abs=1e-4)
        assert ts["avg_value"] == pytest.approx(0.028, abs=1e-3)
        assert ts["latest_value"] == pytest.approx(0.03, abs=1e-4)
        assert len(ts["data_points"]) == 5

    def test_anomaly_detection_fires_when_spike(self):
        values = [
            [1733500000, "0.02"],
            [1733500015, "0.02"],
            [1733500030, "0.02"],
            [1733500045, "0.02"],
            [1733500060, "0.20"],
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = make_prom_response(values)
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            ts = self.client.query_error_rate("service_b", lookback_minutes=5)

        assert ts["is_anomalous"] is True

    def test_anomaly_detection_does_not_fire_for_normal_values(self):
        values = [
            [1733500000, "0.02"],
            [1733500015, "0.022"],
            [1733500030, "0.019"],
            [1733500045, "0.021"],
            [1733500060, "0.020"],
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = make_prom_response(values)
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            ts = self.client.query_error_rate("service_b", lookback_minutes=5)

        assert ts["is_anomalous"] is False

    def test_network_error_raises_query_error(self):
        with patch.object(self.client._client, "get",
                          side_effect=httpx.ConnectError("refused")):
            with pytest.raises(QueryError) as exc_info:
                self.client.query_error_rate("service_a")

        assert exc_info.value.backend == "prometheus"

    def test_nan_values_filtered(self):
        values = [
            [1733500000, "0.05"],
            [1733500015, "NaN"],
            [1733500030, "0.06"],
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = make_prom_response(values)
        mock_response.raise_for_status = MagicMock()

        with patch.object(self.client._client, "get", return_value=mock_response):
            ts = self.client.query_error_rate("service_b", lookback_minutes=5)

        assert len(ts["data_points"]) == 2
        assert all(dp["value"] == dp["value"] for dp in ts["data_points"])
