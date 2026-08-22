"""
Tests for the team-scoped rate limit Prometheus gauges.

LiteLLM exposes configured/remaining rate limits at virtual key scope
(``litellm_remaining_api_key_*_for_model``) and deployment scope
(``litellm_deployment_{tpm,rpm}_limit``) but not at team scope, so there is no
way to alert on a team approaching the ``model_tpm_limit`` / ``model_rpm_limit``
configured on its team object.

The v3 rate limiter already computes those numbers for its ``model_per_team``
descriptor and ships them to clients as
``x-ratelimit-model_per_team-{remaining,limit}-{requests,tokens}``. These tests
cover routing those already-computed values to Prometheus.
"""

from typing import get_args
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import CollectorRegistry, Gauge

from litellm.integrations.prometheus import PrometheusLogger
from litellm.types.integrations.prometheus import (
    DEFINED_PROMETHEUS_METRICS,
    NoOpMetric,
    PrometheusMetricLabels,
    UserAPIKeyLabelNames,
)

TEAM_LABELS = {"team": "team-abc", "team_alias": "research", "model": "gpt-4o-mini"}
LABELNAMES = ("team", "team_alias", "model")

TEAM_RATE_LIMIT_METRICS = (
    "litellm_remaining_team_requests_for_model",
    "litellm_remaining_team_tokens_for_model",
    "litellm_team_rpm_limit",
    "litellm_team_tpm_limit",
)

ALL_TEAM_HEADERS = {
    "x-ratelimit-model_per_team-remaining-requests": 42,
    "x-ratelimit-model_per_team-remaining-tokens": 900,
    "x-ratelimit-model_per_team-limit-requests": 100,
    "x-ratelimit-model_per_team-limit-tokens": 1000,
}


def _logger_with_mock_team_gauges() -> PrometheusLogger:
    with patch("litellm.integrations.prometheus.PrometheusLogger.__init__", return_value=None):
        logger = PrometheusLogger()
    for metric_name in TEAM_RATE_LIMIT_METRICS:
        setattr(logger, metric_name, MagicMock())
    logger.get_labels_for_metric = MagicMock(side_effect=PrometheusMetricLabels.get_labels)
    return logger


def _payload_with_headers(additional_headers: dict) -> dict:
    return {"metadata": {}, "hidden_params": {"additional_headers": additional_headers}}


def _set_team_metrics(logger, standard_logging_payload, team_alias="research"):
    logger._set_team_rate_limit_metrics(
        user_api_team="team-abc",
        user_api_team_alias=team_alias,
        model_group="gpt-4o-mini",
        combined_metadata={},
        standard_logging_payload=standard_logging_payload,
    )


def _assert_set_once(logger, metric_name, value):
    getattr(logger, metric_name).labels.return_value.set.assert_called_once_with(value)


def test_team_metrics_are_defined_with_team_and_model_labels():
    defined_metrics = get_args(DEFINED_PROMETHEUS_METRICS)
    expected_labels = [
        UserAPIKeyLabelNames.TEAM.value,
        UserAPIKeyLabelNames.TEAM_ALIAS.value,
        UserAPIKeyLabelNames.v1_LITELLM_MODEL_NAME.value,
    ]

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        assert metric_name in defined_metrics
        labels = PrometheusMetricLabels.get_labels(metric_name)
        for expected_label in expected_labels:
            assert expected_label in labels


def test_every_logger_owned_metric_resolves_labels():
    """
    ``PrometheusMetricLabels.get_labels`` resolves a metric name to a label
    list via ``getattr``, so a metric added to the literal without a matching
    label attribute fails at logger construction time in production.

    ``litellm_in_flight_requests`` is excluded because it is a label-free gauge
    registered by the in-flight middleware, not by ``PrometheusLogger``.
    """
    for metric_name in get_args(DEFINED_PROMETHEUS_METRICS):
        if metric_name == "litellm_in_flight_requests":
            continue
        assert isinstance(PrometheusMetricLabels.get_labels(metric_name), list)


def test_sets_every_team_gauge_from_v3_headers():
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(logger, _payload_with_headers(dict(ALL_TEAM_HEADERS)))

    _assert_set_once(logger, "litellm_remaining_team_requests_for_model", 42)
    _assert_set_once(logger, "litellm_remaining_team_tokens_for_model", 900)
    _assert_set_once(logger, "litellm_team_rpm_limit", 100)
    _assert_set_once(logger, "litellm_team_tpm_limit", 1000)


def test_labels_carry_team_and_requested_model():
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(logger, _payload_with_headers(dict(ALL_TEAM_HEADERS)))

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        labelnames = PrometheusMetricLabels.get_labels(metric_name)
        label_values = getattr(logger, metric_name).labels.call_args.args
        assert dict(zip(labelnames, label_values, strict=True)) == TEAM_LABELS


def test_emits_nothing_when_team_has_no_configured_limits():
    """A team without per-model limits gets no descriptor, so no header, so no series."""
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(
        logger,
        _payload_with_headers(
            {
                "x-ratelimit-model_per_key-remaining-requests": 42,
                "x-ratelimit-model_per_key-limit-requests": 100,
            }
        ),
    )

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        getattr(logger, metric_name).labels.assert_not_called()


def test_drops_stale_series_when_a_team_limit_is_removed():
    """
    Prometheus keeps a child series for the life of the process once emitted,
    so a team whose limit is removed would otherwise keep publishing the last
    values it saw and alerts would fire on a limit nobody enforces.
    """
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(logger, _payload_with_headers(dict(ALL_TEAM_HEADERS)))
    _assert_set_once(logger, "litellm_remaining_team_requests_for_model", 42)

    _set_team_metrics(logger, _payload_with_headers({}))

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        getattr(logger, metric_name).remove.assert_called_once_with("team-abc", "research", "gpt-4o-mini")


def test_survives_removing_a_series_that_was_never_emitted():
    """The common case: a team that never had a limit for this model."""
    logger = _logger_with_mock_team_gauges()
    for metric_name in TEAM_RATE_LIMIT_METRICS:
        getattr(logger, metric_name).remove.side_effect = KeyError("not present")

    _set_team_metrics(logger, _payload_with_headers({}))

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        getattr(logger, metric_name).labels.assert_not_called()


def test_emits_only_the_dimension_the_team_configured():
    """A team with only an RPM limit must not get a fabricated TPM series."""
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(
        logger,
        _payload_with_headers(
            {
                "x-ratelimit-model_per_team-remaining-requests": 7,
                "x-ratelimit-model_per_team-limit-requests": 60,
            }
        ),
    )

    _assert_set_once(logger, "litellm_remaining_team_requests_for_model", 7)
    _assert_set_once(logger, "litellm_team_rpm_limit", 60)
    logger.litellm_remaining_team_tokens_for_model.labels.assert_not_called()
    logger.litellm_team_tpm_limit.labels.assert_not_called()


def test_emits_zero_remaining_rather_than_skipping_it():
    """An exhausted team is the case operators alert on, so 0 must be a real sample."""
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(
        logger,
        _payload_with_headers(
            {
                "x-ratelimit-model_per_team-remaining-requests": 0,
                "x-ratelimit-model_per_team-remaining-tokens": 0,
            }
        ),
    )

    _assert_set_once(logger, "litellm_remaining_team_requests_for_model", 0)
    _assert_set_once(logger, "litellm_remaining_team_tokens_for_model", 0)


def test_emits_nothing_for_a_request_with_no_team():
    logger = _logger_with_mock_team_gauges()

    logger._set_team_rate_limit_metrics(
        user_api_team=None,
        user_api_team_alias=None,
        model_group="gpt-4o-mini",
        combined_metadata={},
        standard_logging_payload=_payload_with_headers(dict(ALL_TEAM_HEADERS)),
    )

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        getattr(logger, metric_name).labels.assert_not_called()


@pytest.mark.parametrize("bad_value", ["100", None, True, 12.5])
def test_ignores_non_int_header_values(bad_value):
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(
        logger,
        _payload_with_headers({"x-ratelimit-model_per_team-remaining-requests": bad_value}),
    )

    logger.litellm_remaining_team_requests_for_model.labels.assert_not_called()


def test_raises_nothing_when_payload_has_no_hidden_params():
    logger = _logger_with_mock_team_gauges()

    _set_team_metrics(logger, {"metadata": {}})

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        getattr(logger, metric_name).labels.assert_not_called()


def _logger_with_real_gauge(metric_name, gauge):
    logger = _logger_with_mock_team_gauges()
    setattr(logger, metric_name, gauge)
    return logger


def test_removes_a_real_prometheus_child_series_when_the_limit_disappears():
    """
    Mock gauges cannot prove the drop works, since prometheus_client owns the
    child-series bookkeeping. This drives the real Gauge end to end.
    """
    registry = CollectorRegistry()
    gauge = Gauge("litellm_team_rpm_limit", "doc", labelnames=list(LABELNAMES), registry=registry)
    logger = _logger_with_real_gauge("litellm_team_rpm_limit", gauge)

    _set_team_metrics(logger, _payload_with_headers({"x-ratelimit-model_per_team-limit-requests": 60}))
    assert registry.get_sample_value("litellm_team_rpm_limit", TEAM_LABELS) == 60

    _set_team_metrics(logger, _payload_with_headers({}))
    assert registry.get_sample_value("litellm_team_rpm_limit", TEAM_LABELS) is None


def test_drops_the_stale_series_of_a_team_that_has_no_alias():
    """
    A team with no alias labels the series with whatever the label factory
    produces for a missing value. If the set and remove paths disagree on how
    to spell that, the series is written under one labelset and looked up under
    another, and never gets retired.
    """
    registry = CollectorRegistry()
    gauge = Gauge("litellm_team_rpm_limit", "doc", labelnames=list(LABELNAMES), registry=registry)
    logger = _logger_with_real_gauge("litellm_team_rpm_limit", gauge)

    _set_team_metrics(
        logger,
        _payload_with_headers({"x-ratelimit-model_per_team-limit-requests": 60}),
        team_alias=None,
    )
    written = [sample.labels for metric in gauge.collect() for sample in metric.samples]
    assert len(written) == 1

    _set_team_metrics(logger, _payload_with_headers({}), team_alias=None)

    assert not [sample for metric in gauge.collect() for sample in metric.samples]


def test_removing_a_never_emitted_real_series_raises_nothing():
    registry = CollectorRegistry()
    gauge = Gauge("litellm_team_tpm_limit", "doc", labelnames=list(LABELNAMES), registry=registry)
    logger = _logger_with_real_gauge("litellm_team_tpm_limit", gauge)

    _set_team_metrics(logger, _payload_with_headers({}))

    assert registry.get_sample_value("litellm_team_tpm_limit", TEAM_LABELS) is None


def test_noop_metric_remove_is_inert():
    """A disabled metric answers every call without recording or raising."""
    metric = NoOpMetric()

    child = metric.labels(*TEAM_LABELS.values())

    assert child is metric
    assert child.set(60) is None
    assert metric.remove(*TEAM_LABELS.values()) is None


def test_retires_the_old_series_when_a_team_is_renamed():
    """
    A rename changes team_alias, which starts a new series. The old one would
    otherwise keep publishing the values it held at rename time, double
    counting the team on any sum over `team`.
    """
    registry = CollectorRegistry()
    gauge = Gauge("litellm_team_rpm_limit", "doc", labelnames=list(LABELNAMES), registry=registry)
    logger = _logger_with_real_gauge("litellm_team_rpm_limit", gauge)
    payload = _payload_with_headers({"x-ratelimit-model_per_team-limit-requests": 60})

    _set_team_metrics(logger, payload)
    assert registry.get_sample_value("litellm_team_rpm_limit", TEAM_LABELS) == 60

    _set_team_metrics(logger, payload, team_alias="ml-research")

    renamed = {**TEAM_LABELS, "team_alias": "ml-research"}
    assert registry.get_sample_value("litellm_team_rpm_limit", renamed) == 60
    assert registry.get_sample_value("litellm_team_rpm_limit", TEAM_LABELS) is None


def test_keeps_other_teams_when_one_team_is_renamed():
    registry = CollectorRegistry()
    gauge = Gauge("litellm_team_rpm_limit", "doc", labelnames=list(LABELNAMES), registry=registry)
    logger = _logger_with_real_gauge("litellm_team_rpm_limit", gauge)
    payload = _payload_with_headers({"x-ratelimit-model_per_team-limit-requests": 60})

    logger._set_team_rate_limit_metrics(
        user_api_team="team-other",
        user_api_team_alias="platform",
        model_group="gpt-4o-mini",
        combined_metadata={},
        standard_logging_payload=payload,
    )
    _set_team_metrics(logger, payload)
    _set_team_metrics(logger, payload, team_alias="ml-research")

    other = {"team": "team-other", "team_alias": "platform", "model": "gpt-4o-mini"}
    assert registry.get_sample_value("litellm_team_rpm_limit", other) == 60


def test_emits_nothing_when_the_team_label_is_filtered_out():
    """
    Without a team label the gauge collapses to one sample shared by every
    team, which attributes a limit to nobody and cannot be retired.
    """
    logger = _logger_with_mock_team_gauges()
    logger.get_labels_for_metric = MagicMock(return_value=["model"])

    _set_team_metrics(logger, _payload_with_headers(dict(ALL_TEAM_HEADERS)))

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        getattr(logger, metric_name).labels.assert_not_called()
        getattr(logger, metric_name).remove.assert_not_called()


def test_label_filters_narrow_team_gauges_without_dropping_the_team_label():
    """
    ``prometheus_metrics_config`` can restrict a metric's labels. The team
    gauges stay usable as long as ``team`` survives the filter.
    """
    with patch("litellm.integrations.prometheus.PrometheusLogger.__init__", return_value=None):
        logger = PrometheusLogger()
    logger.label_filters = {name: ["team", "model"] for name in TEAM_RATE_LIMIT_METRICS}

    for metric_name in TEAM_RATE_LIMIT_METRICS:
        assert logger.get_labels_for_metric(metric_name) == ["team", "model"]
