# LiteLLM v1.82.3 with the team per-model rate limit fixes applied.
#
# Nothing is downloaded or compiled here: the patches are applied to the
# LiteLLM already installed in the official image, verified by SHA-256 before
# and after, so the build is hermetic and reproducible from the base digest.
# Pin BASE_IMAGE to a digest for a repeatable build; override RUNTIME_USER when
# basing on the non-root image so the patch layer does not leave it running as
# root.
ARG BASE_IMAGE=ghcr.io/berriai/litellm:v1.82.3

FROM ${BASE_IMAGE}

ARG RUNTIME_USER=root
USER root

WORKDIR /opt/litellm-patches
COPY manifest.json apply_patches.py ./
COPY patches/ ./patches/
COPY tests/ ./tests/

# The second, writeless run re-checks every patched file against the manifest,
# so the layer is verified as well as applied.
RUN python3 apply_patches.py && python3 apply_patches.py --dry-run

# Import the patched modules once, so a syntax or import error fails the build
# rather than the first request in production.
RUN python3 -c "\
from typing import get_args;\
import litellm.integrations.prometheus as prometheus;\
import litellm.proxy.hooks.parallel_request_limiter_v3 as limiter;\
from litellm.litellm_core_utils.litellm_logging import StandardLoggingPayloadSetup;\
from litellm.types.integrations.prometheus import DEFINED_PROMETHEUS_METRICS;\
assert hasattr(limiter._PROXY_MaxParallelRequestsHandler_v3, '_deduplicate_descriptors');\
assert hasattr(prometheus.PrometheusLogger, '_set_team_rate_limit_metrics');\
assert 'litellm_team_rpm_limit' in get_args(DEFINED_PROMETHEUS_METRICS);\
assert StandardLoggingPayloadSetup.get_additional_headers({'x-ratelimit-model_per_team-limit-requests': 4}) == {'x-ratelimit-model_per_team-limit-requests': 4};\
print('patched litellm imports cleanly')"

WORKDIR /app
USER ${RUNTIME_USER}
