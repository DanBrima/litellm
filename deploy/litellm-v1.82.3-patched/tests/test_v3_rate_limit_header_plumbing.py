"""
Tests for the transport between the v3 rate limiter and the Prometheus gauges.

The limiter reports every scope it enforced on the response as
``x-ratelimit-{descriptor_key}-{remaining,limit}-{requests,tokens}``. Stock
1.82.3 copies only four hard-coded header names into the standard logging
payload, so those per-scope values never reached any logger and the team gauges
would publish nothing at all. The end-to-end test here is what proves the whole
chain, from a rate-limited request to a scrape of ``/metrics``.
"""

from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import CollectorRegistry, Gauge, generate_latest

from litellm.caching.dual_cache import DualCache
from litellm.integrations.prometheus import PrometheusLogger
from litellm.litellm_core_utils.litellm_logging import StandardLoggingPayloadSetup
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.parallel_request_limiter_v3 import (
    _PROXY_MaxParallelRequestsHandler_v3,
)
from litellm.proxy.utils import InternalUsageCache
from litellm.types.integrations.prometheus import PrometheusMetricLabels
from litellm.types.utils import ModelResponse

MODEL = "gpt-4o-mini"
TEAM_ID = "team-abc"
RPM = 4
TPM = 1000

TEAM_GAUGES = {
    "litellm_remaining_team_requests_for_model": "x-ratelimit-model_per_team-remaining-requests",
    "litellm_remaining_team_tokens_for_model": "x-ratelimit-model_per_team-remaining-tokens",
    "litellm_team_rpm_limit": "x-ratelimit-model_per_team-limit-requests",
    "litellm_team_tpm_limit": "x-ratelimit-model_per_team-limit-tokens",
}


def test_per_scope_rate_limit_headers_survive_into_the_logging_payload():
    hidden_params = StandardLoggingPayloadSetup.get_hidden_params(
        {
            "additional_headers": {
                "x-ratelimit-model_per_team-remaining-requests": 3,
                "x-ratelimit-model_per_team-limit-requests": 4,
            }
        }
    )

    assert hidden_params["additional_headers"]["x-ratelimit-model_per_team-remaining-requests"] == 3
    assert hidden_params["additional_headers"]["x-ratelimit-model_per_team-limit-requests"] == 4


def test_typed_headers_are_still_coerced_to_their_declared_names():
    hidden_params = StandardLoggingPayloadSetup.get_hidden_params(
        {"additional_headers": {"x-ratelimit-remaining-requests": "17"}}
    )

    assert hidden_params["additional_headers"]["x_ratelimit_remaining_requests"] == 17
    assert "x-ratelimit-remaining-requests" not in hidden_params["additional_headers"]


def test_headers_outside_the_rate_limit_family_are_still_dropped():
    """The passthrough is deliberately narrow: no provider headers get logged."""
    hidden_params = StandardLoggingPayloadSetup.get_hidden_params(
        {
            "additional_headers": {
                "set-cookie": "session=secret",
                "llm_provider-x-request-id": "req_123",
                "content-type": "application/json",
            }
        }
    )

    assert hidden_params["additional_headers"] == {}


def test_no_additional_headers_stays_none():
    assert StandardLoggingPayloadSetup.get_additional_headers(None) is None


def _team_auth():
    return UserAPIKeyAuth(
        api_key="sk-test",
        team_id=TEAM_ID,
        team_alias="research",
        team_metadata={"model_rpm_limit": {MODEL: RPM}, "model_tpm_limit": {MODEL: TPM}},
    )


def _logger_with_registry(registry):
    with patch("litellm.integrations.prometheus.PrometheusLogger.__init__", return_value=None):
        logger = PrometheusLogger()
    logger.get_labels_for_metric = MagicMock(side_effect=PrometheusMetricLabels.get_labels)
    for metric_name in TEAM_GAUGES:
        setattr(
            logger,
            metric_name,
            Gauge(
                metric_name,
                "doc",
                labelnames=PrometheusMetricLabels.get_labels(metric_name),
                registry=registry,
            ),
        )
    return logger


async def _rate_limited_request_headers(handler, auth):
    """Drive the real limiter hooks in the order the proxy calls them."""
    data = {"model": MODEL}
    await handler.async_pre_call_hook(
        user_api_key_dict=auth, cache=DualCache(), data=data, call_type="completion"
    )
    response = ModelResponse()
    response._hidden_params = {}
    await handler.async_post_call_success_hook(data=data, user_api_key_dict=auth, response=response)
    return response._hidden_params.get("additional_headers", {})


@pytest.mark.asyncio
async def test_team_gauges_reach_a_metrics_scrape_from_a_real_rate_limited_request():
    """
    The whole chain in one test: limiter pre-call and post-call hooks, the
    standard logging payload the proxy hands the logger, the gauges, and the
    Prometheus text exposition an operator's scrape would read.
    """
    handler = _PROXY_MaxParallelRequestsHandler_v3(
        internal_usage_cache=InternalUsageCache(dual_cache=DualCache())
    )
    registry = CollectorRegistry()
    logger = _logger_with_registry(registry)

    headers = await _rate_limited_request_headers(handler, _team_auth())
    standard_logging_payload = {
        "metadata": {},
        "hidden_params": StandardLoggingPayloadSetup.get_hidden_params({"additional_headers": headers}),
    }

    logger._set_team_rate_limit_metrics(
        user_api_team=TEAM_ID,
        user_api_team_alias="research",
        model_group=MODEL,
        combined_metadata={},
        standard_logging_payload=standard_logging_payload,
    )

    labels = {"team": TEAM_ID, "team_alias": "research", "model": MODEL}
    assert registry.get_sample_value("litellm_team_rpm_limit", labels) == RPM
    assert registry.get_sample_value("litellm_team_tpm_limit", labels) == TPM
    # One request in: the team has spent exactly one of its RPM budget.
    assert registry.get_sample_value("litellm_remaining_team_requests_for_model", labels) == RPM - 1

    exposition = generate_latest(registry).decode()
    for metric_name in TEAM_GAUGES:
        assert f'{metric_name}{{model="{MODEL}",team="{TEAM_ID}",team_alias="research"}}' in exposition


@pytest.mark.asyncio
async def test_remaining_requests_gauge_tracks_consecutive_requests():
    handler = _PROXY_MaxParallelRequestsHandler_v3(
        internal_usage_cache=InternalUsageCache(dual_cache=DualCache())
    )
    registry = CollectorRegistry()
    logger = _logger_with_registry(registry)
    labels = {"team": TEAM_ID, "team_alias": "research", "model": MODEL}

    observed = []
    for _ in range(RPM):
        headers = await _rate_limited_request_headers(handler, _team_auth())
        logger._set_team_rate_limit_metrics(
            user_api_team=TEAM_ID,
            user_api_team_alias="research",
            model_group=MODEL,
            combined_metadata={},
            standard_logging_payload={
                "metadata": {},
                "hidden_params": StandardLoggingPayloadSetup.get_hidden_params(
                    {"additional_headers": headers}
                ),
            },
        )
        observed.append(registry.get_sample_value("litellm_remaining_team_requests_for_model", labels))

    assert observed == [RPM - 1, RPM - 2, RPM - 3, RPM - 4]


@pytest.mark.asyncio
async def test_a_team_without_per_model_limits_publishes_no_team_series():
    handler = _PROXY_MaxParallelRequestsHandler_v3(
        internal_usage_cache=InternalUsageCache(dual_cache=DualCache())
    )
    registry = CollectorRegistry()
    logger = _logger_with_registry(registry)
    auth = UserAPIKeyAuth(api_key="sk-test", team_id=TEAM_ID, team_alias="research")

    headers = await _rate_limited_request_headers(handler, auth)
    logger._set_team_rate_limit_metrics(
        user_api_team=TEAM_ID,
        user_api_team_alias="research",
        model_group=MODEL,
        combined_metadata={},
        standard_logging_payload={
            "metadata": {},
            "hidden_params": StandardLoggingPayloadSetup.get_hidden_params(
                {"additional_headers": headers}
            ),
        },
    )

    exposition = generate_latest(registry).decode()
    for metric_name in TEAM_GAUGES:
        assert f"{metric_name}{{" not in exposition
