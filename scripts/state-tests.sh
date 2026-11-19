#!/usr/bin/env bash
# The state store against real Valkey (standalone; behind three Sentinels) and real Redis (standalone; behind one Sentinel,
# as the platform runs it).
#
#   scripts/state-tests.sh                 # the contract suite and the replica tests
#   scripts/state-tests.sh tests/unit/x.py # or what you name
#
# Valkey runs in containers on a Docker network, and so do the tests, because a Sentinel answers with the
# master's host NAME, which only that network resolves. VKTEAMS_TEST_REQUIRE_VALKEY=1 turns "no Valkey to talk
# to" from a skip into a failure: a contract suite that skipped everything would pass without testing anything.
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -f tests/state/compose.yml)
cleanup() { "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
"${compose[@]}" up -d --wait
targets=("$@")
if [ "${#targets[@]}" -eq 0 ]; then
  targets=(tests/unit/test_state_contract.py tests/unit/test_replicas_on_valkey.py)
fi
docker run --rm --network vkteams-state-tests_default -v "$PWD":/src:ro -w /src \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uv-cache -e PYTHONDONTWRITEBYTECODE=1 \
  -e VKTEAMS_TEST_VALKEY=valkey-master:6379 \
  -e VKTEAMS_TEST_SENTINELS=sentinel-1:26379,sentinel-2:26379,sentinel-3:26379 \
  -e VKTEAMS_TEST_SENTINEL_MASTER=mymaster \
  -e VKTEAMS_TEST_REDIS=redis-master:6379 \
  -e VKTEAMS_TEST_REDIS_SENTINELS=redis-sentinel:26379 \
  -e VKTEAMS_TEST_REQUIRE_VALKEY=1 \
  python:3.12-slim sh -ec 'pip install -q uv==0.8.* && uv sync --frozen -q && uv run pytest -q -p no:cacheprovider "$@"' sh "${targets[@]}"
