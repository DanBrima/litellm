# LiteLLM v1.82.3, patched for team per-model rate limits

Builds a drop-in replacement for `ghcr.io/berriai/litellm-non_root:main-v1.82.3`
carrying two changes that are still open upstream, and the one piece of plumbing
1.82.3 is missing for the second of them to do anything

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

These track the fork branches, re-derived against 1.82.3 rather than
cherry-picked, because both files drifted substantially between 1.82.3 and the
branch base:

- patch 1 tracks `litellm_rate_limiter_dedup` at `b3cc323`, including its later
  rewrite of `_deduplicate_descriptors` to build an immutable index and return a
  tuple. That version collapses a repeated `(key, value)` to the **last**
  occurrence rather than the first, which is what a dict comprehension does; on
  1.82.3 the two `model_per_team` descriptors are byte-identical, so which one
  survives makes no difference to behaviour. Returning a tuple means the two
  consumers that receive it, `should_rate_limit` and `_handle_rate_limit_error`,
  take a `Sequence` here as they do upstream; neither mutates what it is given
- patch 3 tracks `litellm_team_rate_limit_prometheus_metrics` at `7cb2641`
  (PR #37215), including its later switch to passing gauge label values
  positionally and computing them once. That switch matters here beyond style:
  the keyword form wrote the series under whatever the label factory produced
  for a missing value while `remove` looked it up as an empty string, so a team
  with no alias published a series that could never be retired. 1.82.3 has no
  `_ExcludedLabelMetric`, no label context on `prometheus_label_factory` and no
  v3 header reader, so the port keeps the design and rewrites it in the idiom of
  the older file

Patch 2 is not part of either upstream PR. It is needed because 1.82.3 copies
only four hard-coded header names into the standard logging payload, and the
per-scope headers the gauges read are not among them. On current upstream main
that filter already keeps every header verbatim, so patch 2 is a narrowed
backport of behaviour that shipped later: it keeps `x-ratelimit-*` only, and
provider headers are still dropped. Without it patch 3 applies cleanly, imports
cleanly, and silently publishes nothing

## Getting it onto a host

`make_bundle.sh` packs everything here into one tar. `build.sh` turns that into
the image. They are separate because the machine this was prepared on cannot
reach a container registry, so the tar is the handoff:

```bash
./make_bundle.sh                       # dist/litellm-v1.82.3-team-rate-limits.tar.gz + .sha256

# on a host that can pull ghcr.io
tar xzf litellm-v1.82.3-team-rate-limits.tar.gz
cd litellm-v1.82.3-team-rate-limits
./build.sh                             # dist/litellm-v1.82.3-team-rate-limits.tar + .sha256
docker load -i dist/litellm-v1.82.3-team-rate-limits.tar
```

The bundle is byte-for-byte reproducible, so its `.sha256` is worth comparing
after the transfer. `build.sh` checks the built image runs as the expected user,
re-verifies every patched file against the manifest inside the container, and
runs the test suite there before saving the tar

Knobs on `build.sh`, all environment variables:

- `BASE_IMAGE` (default `ghcr.io/berriai/litellm-non_root:main-v1.82.3`) and
  `RUNTIME_USER` (default `nobody`). Override both together to build on a
  different variant; the root image is `ghcr.io/berriai/litellm:v1.82.3` with
  `RUNTIME_USER=root`. To pin the base by digest instead of tag, today's
  `main-v1.82.3` is
  `sha256:3e55452ab78e6ee477b1ae8dade43de7c7aac1d22f3af2e9cf405e7ed3eedc7c`
- `IMAGE_TAG`, `OUT_DIR`, `TAR_NAME`
- `SKIP_TESTS=1` skips the in-container pytest run, which is the only build step
  that needs network. The SHA-256, runtime-user and import checks still run

The build fails loudly rather than shipping a half-patched image if the base is
not stock 1.82.3, if a patch does not apply exactly, if the patched modules do
not import, or if the image would run as the wrong user

Patching happens as root because site-packages is not writable by `nobody`, and
the layer switches back to `RUNTIME_USER` at the end. Each patched module's
bytecode is recompiled during the build for the same reason: `nobody` cannot
write `.pyc` files at runtime, so a stale cache would otherwise be recompiled on
every container start

`build.sh` has not been run end to end yet, because this session's egress policy
blocks the registry blob hosts for both ghcr.io and Docker Hub, so no base image
can be pulled here. Everything the build does to LiteLLM was instead run
directly against a real 1.82.3 install, which is what the validation below
covers

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

**45 tests** in `tests/`, covering the descriptor dedup, the header
passthrough, the four gauges, stale-series removal on limit removal, on team
rename and for a team with no alias, and the whole chain from a rate-limited
request to a `/metrics` scrape. Against stock 1.82.3, 37 of them fail; against
the patched tree all 45 pass

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
scrape. Run for a team with an alias and a team without one, since those take
different paths through the label factory:

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
