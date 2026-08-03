"""Prometheus instrumentation: /metrics endpoint and the custom chat metrics
that infra/k8s/common/monitoring/grafana-agent-dashboard.yaml expects.

The metric objects in app.py are module-level globals shared by the whole
test session (other test files also drive /chat successfully through the
shared `client` fixture), so assertions here compare before/after deltas
rather than absolute values.
"""

import pytest

import app as app_module
from tests.conftest import _fake_agent_result


def _metrics_text(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def _sample_value(metric, name_suffix, **labels):
    """Read a metric's current exposed value via the public .collect() API."""
    for family in metric.collect():
        for sample in family.samples:
            if sample.name.endswith(name_suffix) and all(
                sample.labels.get(key) == value for key, value in labels.items()
            ):
                return sample.value
    return 0.0


def test_metrics_endpoint_exposes_expected_metric_names(client):
    text = _metrics_text(client)

    for metric_name in [
        "agent_chat_requests_total",
        "agent_chat_request_duration_seconds",
        "agent_input_tokens_total",
        "agent_output_tokens_total",
    ]:
        assert metric_name in text


def test_successful_chat_increments_success_counter_and_tokens(client, monkeypatch):
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    success_before = _sample_value(
        app_module.AGENT_CHAT_REQUESTS_TOTAL, "_total", status="success"
    )
    input_tokens_before = _sample_value(app_module.AGENT_INPUT_TOKENS_TOTAL, "_total")
    output_tokens_before = _sample_value(app_module.AGENT_OUTPUT_TOKENS_TOTAL, "_total")
    duration_count_before = _sample_value(
        app_module.AGENT_CHAT_REQUEST_DURATION_SECONDS, "_count"
    )

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-metrics-success",
            "messages": [{"role": "user", "content": "What is in this image?"}],
        },
    )
    assert response.status_code == 200

    # _fake_agent_result() reports tokens_used = {"input": 312, "output": 22}.
    assert _sample_value(
        app_module.AGENT_CHAT_REQUESTS_TOTAL, "_total", status="success"
    ) == success_before + 1
    assert (
        _sample_value(app_module.AGENT_INPUT_TOKENS_TOTAL, "_total")
        == input_tokens_before + 312
    )
    assert (
        _sample_value(app_module.AGENT_OUTPUT_TOKENS_TOTAL, "_total")
        == output_tokens_before + 22
    )
    assert (
        _sample_value(app_module.AGENT_CHAT_REQUEST_DURATION_SECONDS, "_count")
        == duration_count_before + 1
    )


def test_failed_chat_increments_error_counter_and_reraises(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("agent loop exploded")

    monkeypatch.setattr(app_module, "run_agent", _boom)

    error_before = _sample_value(
        app_module.AGENT_CHAT_REQUESTS_TOTAL, "_total", status="error"
    )

    # TestClient re-raises unhandled server exceptions by default
    # (raise_server_exceptions=True), it does not turn them into a response.
    with pytest.raises(RuntimeError, match="agent loop exploded"):
        client.post(
            "/chat",
            json={
                "chat_id": "chat-metrics-error",
                "messages": [{"role": "user", "content": "This will fail"}],
            },
        )

    assert (
        _sample_value(app_module.AGENT_CHAT_REQUESTS_TOTAL, "_total", status="error")
        == error_before + 1
    )
