#!/usr/bin/env bash
# Start, stop or inspect the local stack from inside the devcontainer.
#   scripts/stack.sh up      build and start, then wait until both services answer
#   scripts/stack.sh down    stop (data volumes are kept)
#   scripts/stack.sh logs    follow the proxy and Hindsight logs
#   scripts/stack.sh token T set a fresh upstream SSO token and restart the proxy
#   scripts/stack.sh models  list the models your upstream proxy offers
set -euo pipefail
cd "$(dirname "$0")/.."

wait_for() {
  local name=$1 url=$2 tries=${3:-180}
  printf 'waiting for %s ' "$name"
  for _ in $(seq "$tries"); do
    if curl -fs -o /dev/null "$url"; then echo ' ready'; return 0; fi
    printf '.'; sleep 2
  done
  echo ' timed out'; return 1
}

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }

check_env() {
  if [ ! -f .env ]; then cp .env.example .env; fi
  if [ "$(env_value LITELLM_CONFIG)" = "litellm.upstream.yaml" ]; then
    local missing=()
    for key in UPSTREAM_LITELLM_URL UPSTREAM_LITELLM_KEY HINDSIGHT_LLM_MODEL; do
      [ -n "$(env_value "$key")" ] || missing+=("$key")
    done
    if [ ${#missing[@]} -gt 0 ]; then
      echo "Upstream mode: set ${missing[*]} in .env first (scripts/stack.sh models lists model names)." >&2
      exit 1
    fi
  elif ! grep -qE '^(ANTHROPIC|OPENAI)_API_KEY=.+' .env; then
    echo "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env, or use upstream mode (see .env.example)." >&2
    exit 1
  fi
}

case "${1:-up}" in
  up)
    check_env
    docker compose up -d --build
    wait_for "LiteLLM proxy" http://localhost:4000/health/liveliness 60
    # The first start downloads Hindsight's models, which can take several minutes.
    wait_for "Hindsight" http://localhost:8888/health/ready 300
    ;;
  down) docker compose down ;;
  logs) docker compose logs -f litellm hindsight ;;
  token)
    token=${2:-}
    if [ -z "$token" ]; then read -rsp "New upstream token: " token; echo; fi
    [ -n "$token" ] || { echo "no token given" >&2; exit 1; }
    if grep -q '^UPSTREAM_LITELLM_KEY=' .env; then
      # Write through a temp file so the token never appears in a sed expression.
      awk -v t="$token" 'BEGIN{FS=OFS="="} /^UPSTREAM_LITELLM_KEY=/{print "UPSTREAM_LITELLM_KEY", t; next} {print}' .env > .env.tmp
      mv .env.tmp .env
    else
      echo "UPSTREAM_LITELLM_KEY=$token" >> .env
    fi
    docker compose up -d litellm
    wait_for "LiteLLM proxy" http://localhost:4000/health/liveliness 60
    ;;
  models)
    url=$(env_value UPSTREAM_LITELLM_URL); key=$(env_value UPSTREAM_LITELLM_KEY)
    curl -fsS "${url%/}/v1/models" -H "Authorization: Bearer $key" \
      | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"]]'
    ;;
  *) echo "usage: $0 up|down|logs|token [T]|models" >&2; exit 2 ;;
esac
