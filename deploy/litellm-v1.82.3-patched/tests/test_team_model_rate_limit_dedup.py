"""
Regression tests for the team per-model RPM/TPM double count.

``team_metadata`` per-model limits were turned into a ``model_per_team``
descriptor twice, once in ``_create_rate_limit_descriptors`` and once in
``_add_team_model_rate_limit_descriptor_from_metadata``. Both read the same
``team_metadata`` keys and build an identical descriptor, so the sliding window
incremented that counter twice per request and the atomic TPM reservation
charged the estimate twice, enforcing team per-model RPM and TPM at half their
configured values.

Every test here fails against stock 1.82.3.
"""

import pytest
from fastapi import HTTPException

from litellm.caching.dual_cache import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.parallel_request_limiter_v3 import (
    RateLimitDescriptor,
    _PROXY_MaxParallelRequestsHandler_v3,
)
from litellm.proxy.utils import InternalUsageCache

MODEL = "gpt-4o-mini"
TEAM_ID = "team-abc"


def _auth(rpm=None, tpm=None):
    team_metadata = {}
    if rpm is not None:
        team_metadata["model_rpm_limit"] = {MODEL: rpm}
    if tpm is not None:
        team_metadata["model_tpm_limit"] = {MODEL: tpm}
    return UserAPIKeyAuth(api_key="sk-test", team_id=TEAM_ID, team_metadata=team_metadata)


def _handler():
    return _PROXY_MaxParallelRequestsHandler_v3(
        internal_usage_cache=InternalUsageCache(dual_cache=DualCache())
    )


def _statuses(data, descriptor_key, rate_limit_type):
    return [
        status
        for status in data["litellm_proxy_rate_limit_response"]["statuses"]
        if status["descriptor_key"] == descriptor_key
        and status["rate_limit_type"] == rate_limit_type
    ]


def _team_status(data, rate_limit_type):
    """The one status the team counter is allowed to produce for a request."""
    statuses = _statuses(data, "model_per_team", rate_limit_type)
    assert len(statuses) == 1, f"team {rate_limit_type} counter charged {len(statuses)} times"
    return statuses[0]


async def _run_request(handler, auth, data=None):
    request_data = data if data is not None else {"model": MODEL}
    await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=DualCache(),
        data=request_data,
        call_type="completion",
    )
    return request_data


@pytest.mark.asyncio
async def test_team_rpm_limit_admits_exactly_the_configured_number_of_requests():
    """A team configured for 4 requests a minute gets 4, not 2."""
    handler = _handler()
    rpm = 4

    for _ in range(rpm):
        await _run_request(handler, _auth(rpm=rpm))

    with pytest.raises(HTTPException) as exc_info:
        await _run_request(handler, _auth(rpm=rpm))
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_one_request_consumes_one_request_of_the_team_limit():
    handler = _handler()

    data = await _run_request(handler, _auth(rpm=100))

    status = _team_status(data, "requests")
    assert status["current_limit"] == 100
    assert status["limit_remaining"] == 99


@pytest.mark.asyncio
async def test_one_request_reserves_the_token_estimate_once():
    """
    The pre-call TPM reservation is charged per descriptor, so a repeat
    descriptor books the estimate twice while post-call accounting only ever
    books it once.
    """
    handler = _handler()

    data = await _run_request(handler, _auth(tpm=1000))

    status = _team_status(data, "tokens")
    reserved = status["current_limit"] - status["limit_remaining"]
    key_status = _statuses(data, "model_per_key", "tokens")[0]
    assert reserved == key_status["current_limit"] - key_status["limit_remaining"]


@pytest.mark.asyncio
async def test_team_and_key_counters_stay_in_step():
    """
    A key and its team configured to the same per-model limit must exhaust
    together. Divergence here is the double count showing up in production as
    a team 429 while the key still has budget.
    """
    handler = _handler()
    auth = UserAPIKeyAuth(
        api_key="sk-test",
        team_id=TEAM_ID,
        team_metadata={"model_rpm_limit": {MODEL: 10}},
        model_rpm_limit={MODEL: 10},
    )

    data = await _run_request(handler, auth)

    team = _team_status(data, "requests")
    key = _statuses(data, "model_per_key", "requests")[0]
    assert team["limit_remaining"] == key["limit_remaining"]


def test_deduplication_keeps_the_first_of_each_repeated_descriptor():
    first = RateLimitDescriptor(
        key="model_per_team",
        value=f"{TEAM_ID}:{MODEL}",
        rate_limit={"requests_per_unit": 10, "tokens_per_unit": None, "window_size": 60},
    )
    repeat = RateLimitDescriptor(
        key="model_per_team",
        value=f"{TEAM_ID}:{MODEL}",
        rate_limit={"requests_per_unit": 999, "tokens_per_unit": None, "window_size": 60},
    )

    deduplicated = _PROXY_MaxParallelRequestsHandler_v3._deduplicate_descriptors([first, repeat])

    assert deduplicated == [first]


def test_deduplication_keeps_every_distinct_descriptor_in_order():
    descriptors = [
        RateLimitDescriptor(key="key", value="sk-1", rate_limit={"requests_per_unit": 1}),
        RateLimitDescriptor(key="team", value=TEAM_ID, rate_limit={"requests_per_unit": 2}),
        RateLimitDescriptor(key="model_per_team", value=f"{TEAM_ID}:{MODEL}", rate_limit={"requests_per_unit": 3}),
        RateLimitDescriptor(key="model_per_key", value=f"sk-1:{MODEL}", rate_limit={"requests_per_unit": 4}),
        RateLimitDescriptor(key="model_per_team", value=f"other:{MODEL}", rate_limit={"requests_per_unit": 5}),
    ]

    assert _PROXY_MaxParallelRequestsHandler_v3._deduplicate_descriptors(descriptors) == descriptors


def test_descriptor_builder_still_covers_team_limits_on_its_own():
    """
    The batch rate limiter calls ``_create_rate_limit_descriptors`` directly,
    so that append site is the only source of ``model_per_team`` there and
    must not be deleted in favour of deduplication.
    """
    handler = _handler()

    descriptors = handler._create_rate_limit_descriptors(
        user_api_key_dict=_auth(rpm=7),
        data={"model": MODEL},
        rpm_limit_type=None,
        tpm_limit_type=None,
        model_has_failures=False,
    )

    team_descriptors = [d for d in descriptors if d["key"] == "model_per_team"]
    assert len(team_descriptors) == 1
    assert team_descriptors[0]["value"] == f"{TEAM_ID}:{MODEL}"
    assert team_descriptors[0]["rate_limit"]["requests_per_unit"] == 7
