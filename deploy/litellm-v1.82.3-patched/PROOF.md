# Proof of fix

Both changes, run against a live LiteLLM v1.82.3 proxy backed by a real
Postgres, with a real team object and a real virtual key on that team. The same
script ran twice: once with stock 1.82.3 in site-packages, once with the patches
applied, nothing else different

The upstream deployment is a `mock_response` model rather than a real provider,
because the machine this was prepared on has no provider credentials. Nothing
the patches touch depends on the provider: the rate limiter runs pre-call, the
headers are attached post-call, and the gauges read the logging payload. Every
other layer is real, including proxy auth, the team object loaded from Postgres,
the v3 limiter, the response headers and the `/metrics` endpoint

## Setup

```bash
export DATABASE_URL=postgresql://postgres@127.0.0.1:5433/litellm
export LITELLM_MASTER_KEY=sk-e2e-master
litellm --config config_db.yaml --port 4042
```

`config_db.yaml`:

```yaml
model_list:
  - model_name: fake-gpt
    litellm_params:
      model: openai/fake-gpt
      api_key: sk-not-used
      mock_response: "hello from the mock upstream"

litellm_settings:
  callbacks: ["prometheus"]

general_settings:
  master_key: sk-e2e-master
```

## What was run

A team limited to 4 requests and 500 tokens a minute for one model, a key on
that team, then five identical requests and a `/metrics` scrape:

```bash
curl -s http://127.0.0.1:4042/team/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"team_alias":"proof","model_rpm_limit":{"fake-gpt":4},"model_tpm_limit":{"fake-gpt":500}}'

curl -s http://127.0.0.1:4042/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"team_id":"<team_id>"}'

for i in 1 2 3 4 5; do
  curl -s -o /dev/null -D - -w "request $i -> %{http_code}\n" \
    http://127.0.0.1:4042/v1/chat/completions \
    -H "Authorization: Bearer <key>" -H 'Content-Type: application/json' \
    -d '{"model":"fake-gpt","messages":[{"role":"user","content":"hi"}]}' \
  | grep -iE 'x-ratelimit-model_per_team|request '
done

curl -sL http://127.0.0.1:4042/metrics/ -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | grep -E '^litellm_(remaining_team_(requests|tokens)_for_model|team_[rt]pm_limit)\{'
```

## Stock v1.82.3

```
team_id=b1d10dd3-948e-444a-ad62-460ba9155935   model_rpm_limit=4   model_tpm_limit=500

request   http   rpm-remain rpm-limit tpm-remain tpm-limit
#1        200    2          4        498        500
#2        200    0          4        466        500
#3        429
#4        429
#5        429

/metrics team rate limit series:
  (none published)
```

Two requests admitted against a limit of 4. One request takes the remaining
count from 4 to 2, and the token reservation from 500 to 498 for a one-token
estimate, because the `model_per_team` descriptor is built twice and every
consumer charges once per descriptor. No team rate limit series exist at all

## Patched

```
team_id=ba4cd071-4271-4bd4-8cc7-402ab415f085   model_rpm_limit=4   model_tpm_limit=500

request   http   rpm-remain rpm-limit tpm-remain tpm-limit
#1        200    3          4        499        500
#2        200    2          4        468        500
#3        200    1          4        437        500
#4        200    0          4        406        500
#5        429

/metrics team rate limit series:
  litellm_remaining_team_requests_for_model{model="fake-gpt",team="ba4cd071-...",team_alias="proof-patched"} 0.0
  litellm_remaining_team_tokens_for_model{model="fake-gpt",team="ba4cd071-...",team_alias="proof-patched"} 406.0
  litellm_team_rpm_limit{model="fake-gpt",team="ba4cd071-...",team_alias="proof-patched"} 4.0
  litellm_team_tpm_limit{model="fake-gpt",team="ba4cd071-...",team_alias="proof-patched"} 500.0
```

Four requests admitted against a limit of 4, the fifth rejected. Each request
takes one off the remaining request count, and the first takes one token off the
reservation rather than two. All four gauges are published, and their values
match the headers the last successful request returned

## What each number proves

| Observation | Stock | Patched |
| --- | --- | --- |
| Requests admitted at `model_rpm_limit: 4` | 2 | 4 |
| RPM consumed by one request | 2 | 1 |
| TPM reserved for a one-token estimate | 2 | 1 |
| Team rate limit series on `/metrics` | none | all four |

The TPM row is the one that is easy to miss. The pre-call reservation was
charged per descriptor while post-call accounting only ever booked the estimate
once, so team token limits were not just halved, they drifted from what the
proxy actually recorded as spent

## Also checked

A second patched run with a team that has **no** alias, since that value takes a
different path through the Prometheus label factory. Same rate limiting result,
and its four series are published and can be retired. That case is covered by a
regression test as well, because an earlier version of this backport wrote that
series under one labelset and tried to remove it under another
