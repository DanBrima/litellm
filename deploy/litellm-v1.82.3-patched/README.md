# LiteLLM v1.82.3, patched for team per-model rate limits

Builds a drop-in replacement for `ghcr.io/berriai/litellm:v1.82.3` carrying two
changes that are still open upstream, and the one piece of plumbing 1.82.3 is
missing for the second of them to do anything

Nothing is rebuilt from source. The patches are applied to the LiteLLM already
installed in the official image, each file checked by SHA-256 before and after,
so the build needs no network, no compiler and no `patch` binary, and the result
is reproducible from the base image digest

## What is in here

| Patch | File | What it does |
| --- | --- | --- |
| `0001-rate-limiter-descriptor-dedup.patch` | `litellm/proxy/hooks/parallel_request_limiter_v3.py` | Counts each rate limit descriptor once per request, so team per-model RPM and TPM are enforced at their configured value instead of half of it |
| `0002-keep-v3-rate-limit-headers-in-logging-payload.patch` | `litellm/litellm_core_utils/litellm_logging.py` | Keeps `x-ratelimit-*` headers the v3 limiter emits in the standard logging payload, so loggers can read the per-scope numbers |
| `0003-prometheus-team-rate-limit-gauges.patch` | `litellm/integrations/prometheus.py`, `litellm/types/integrations/prometheus.py` | Adds `litellm_remaining_team_requests_for_model`, `litellm_remaining_team_tokens_for_model`, `litellm_team_rpm_limit`, `litellm_team_tpm_limit` |

Patch 2 is not part of either upstream PR. It is needed because 1.82.3 copies
only four hard-coded header names into the standard logging payload, and the
per-scope headers the gauges read are not among them. On current upstream main
that filter already keeps every header verbatim, so patch 2 is a narrowed
backport of behaviour that shipped later: it keeps `x-ratelimit-*` only, and
provider headers are still dropped. Without it patch 3 applies cleanly, imports
cleanly, and silently publishes nothing

## Building

```bash
./build.sh
```

Writes `dist/litellm-v1.82.3-team-rate-limits.tar` plus a `.sha256`, and loads
onto the target host with `docker load -i litellm-v1.82.3-team-rate-limits.tar`

Knobs, all environment variables:

- `BASE_IMAGE` (default `ghcr.io/berriai/litellm:v1.82.3`). Pin it to a digest
  for a repeatable build, or point it at the non-root or database image variant
- `RUNTIME_USER` (default `root`). Set it to the base image's own user when
  building on the non-root variant, so the patch layer does not leave the image
  running as root
- `IMAGE_TAG`, `OUT_DIR`, `TAR_NAME`
- `SKIP_TESTS=1` skips the in-container pytest run, which is the only build step
  that needs network. The SHA-256 and import checks still run

The build fails loudly rather than shipping a half-patched image if the base is
not stock 1.82.3, if a patch does not apply exactly, or if the patched modules
do not import

`build.sh` has not been run end to end yet: the session this was prepared in
cannot pull any base image, because its egress policy blocks the registry blob
hosts for both ghcr.io and Docker Hub. Everything the build does to LiteLLM was
instead run directly against a real 1.82.3 install, which is what the validation
below covers; the first `./build.sh` needs to happen somewhere that can reach
the registry

## Running the tests

`tests/` ships inside the image at `/opt/litellm-patches/tests`, and
`build.sh` runs it there. Against a local checkout:

```bash
python -m pytest deploy/litellm-v1.82.3-patched/tests -o asyncio_mode=auto
```

They must run against an installed LiteLLM 1.82.3 with the patches applied;
`test_patch_integrity.py` fails first and clearly if they are not

## Validation performed

**Both bugs reproduced on stock 1.82.3 first.** The rate limiter builds the
`model_per_team` descriptor twice per request, and the v3 rate limit headers are
dropped before any logger sees them

**43 tests** in `tests/`, covering the descriptor dedup, the header
passthrough, the four gauges, stale-series removal on limit removal and on team
rename, and the whole chain from a rate-limited request to a `/metrics` scrape.
Against stock 1.82.3, 35 of them fail; against the patched tree all 43 pass

**LiteLLM's own v1.82.3 test suite** for every touched area
(`tests/test_litellm/proxy/hooks`, `tests/test_litellm/integrations`,
`tests/test_litellm/litellm_core_utils`, 1498 tests) run on the v1.82.3 tag with
the CI dependency set from `.circleci/config.yml`, before and after patching:

```
stock v1.82.3    3 failed, 1491 passed, 4 skipped
patched          3 failed, 1491 passed, 4 skipped
```

The same three failures both times, all environmental (two fetch remote
fixtures, one is a HuggingFace tokenizer API drift), so the patches introduce no
regression. Patching also needs the one-line test stub in
`upstream-test-patch/`, which is the same change the upstream PR makes: a test
that mocks out `PrometheusLogger.__init__` has to stub each metric-setting
method it does not exercise

**A live proxy**, v1.82.3 with a real Postgres, a real team object carrying
`model_rpm_limit: {fake-gpt: 4}` and `model_tpm_limit: {fake-gpt: 500}`, and a
real virtual key on that team. Five identical requests, then a `/metrics`
scrape:

```
stock v1.82.3                          patched
request 1 -> HTTP 200  remaining 2     request 1 -> HTTP 200  remaining 3
request 2 -> HTTP 200  remaining 0     request 2 -> HTTP 200  remaining 2
request 3 -> HTTP 429                  request 3 -> HTTP 200  remaining 1
request 4 -> HTTP 429                  request 4 -> HTTP 200  remaining 0
request 5 -> HTTP 429                  request 5 -> HTTP 429
```

```
# stock: no team rate limit series at all
litellm_remaining_team_budget_metric{team="...",team_alias="research-stock2"} +Inf
litellm_teams_count 0.0

# patched
litellm_remaining_team_requests_for_model{model="fake-gpt",team="...",team_alias="research"} 0.0
litellm_remaining_team_tokens_for_model{model="fake-gpt",team="...",team_alias="research"} 406.0
litellm_team_rpm_limit{model="fake-gpt",team="...",team_alias="research"} 4.0
litellm_team_tpm_limit{model="fake-gpt",team="...",team_alias="research"} 500.0
```

The upstream call was a `mock_response` deployment rather than a real provider,
because this session has no provider credentials. Everything the patches touch
runs for real: proxy auth, the team object from Postgres, the v3 limiter, the
response headers, the logging payload, and the `/metrics` endpoint

To reproduce it against a real provider, point a model at one and run:

```bash
curl -s http://localhost:4000/team/new -H "Authorization: Bearer $MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"team_alias":"research","model_rpm_limit":{"gpt-4o-mini":4}}'
curl -s http://localhost:4000/key/generate -H "Authorization: Bearer $MASTER_KEY" \
  -H 'Content-Type: application/json' -d '{"team_id":"<team_id>"}'
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "request $i -> %{http_code}\n" \
    http://localhost:4000/v1/chat/completions -H "Authorization: Bearer <key>" \
    -H 'Content-Type: application/json' \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
done
curl -sL http://localhost:4000/metrics/ -H "Authorization: Bearer $MASTER_KEY" \
  | grep -E '^litellm_(remaining_)?team.*_for_model|^litellm_team_[rt]pm_limit'
```

## Known gap this does not close

The per-virtual-key gauges (`litellm_remaining_api_key_*_for_model`) still read
only the legacy limiter's metadata keys on 1.82.3, so under the v3 limiter they
publish `sys.maxsize`. Upstream fixed that separately, after 1.82.3, and it is
out of scope for these two changes. The team gauges added here read the v3
headers directly and are unaffected

## Retiring this image

Delete the whole directory once a release carrying both upstream PRs is
available, and move back to the stock image. Nothing here is meant to outlive
that
