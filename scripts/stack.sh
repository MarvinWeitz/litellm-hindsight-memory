#!/usr/bin/env bash
# Start, stop or inspect the local stack from inside the devcontainer.
#   scripts/stack.sh up      build and start, then wait until both services answer
#   scripts/stack.sh down    stop (data volumes are kept)
#   scripts/stack.sh logs    follow the proxy and Hindsight logs
set -euo pipefail
cd "$(dirname "$0")/.."

wait_for() {
  local name=$1 url=$2 tries=${3:-180}
  printf 'waiting for %s ' "$name"
  for _ in $(seq "$tries"); do
    if curl -fsS -o /dev/null "$url"; then echo ' ready'; return 0; fi
    printf '.'; sleep 2
  done
  echo ' timed out'; return 1
}

case "${1:-up}" in
  up)
    if [ ! -f .env ]; then cp .env.example .env; fi
    if ! grep -qE '^(ANTHROPIC|OPENAI)_API_KEY=.+' .env; then
      echo "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env first." >&2; exit 1
    fi
    docker compose up -d --build
    wait_for "LiteLLM proxy" http://localhost:4000/health/liveliness 60
    # The first start downloads Hindsight's models, which can take several minutes.
    wait_for "Hindsight" http://localhost:8888/health/ready 300
    ;;
  down) docker compose down ;;
  logs) docker compose logs -f litellm hindsight ;;
  *) echo "usage: $0 up|down|logs" >&2; exit 2 ;;
esac
