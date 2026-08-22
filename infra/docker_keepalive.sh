#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="${SERVICE:-bot}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-30}"
LOG_TAIL="${LOG_TAIL:-80}"

cd "$APP_DIR"

start_docker_if_needed() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi

  echo "Docker daemon is not reachable; trying to start Docker..."
  if [[ "$(uname -s)" == "Darwin" ]]; then
    open -ga Docker || true
  elif command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start docker || true
  fi

  for _ in {1..60}; do
    if docker info >/dev/null 2>&1; then
      echo "Docker daemon is ready."
      return 0
    fi
    sleep 2
  done

  echo "Docker daemon is still not reachable." >&2
  return 1
}

compose_up() {
  docker compose up -d --build "$SERVICE"
}

container_state() {
  local cid="$1"
  docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$cid"
}

start_docker_if_needed
compose_up
docker compose ps "$SERVICE"
docker compose logs --tail="$LOG_TAIL" "$SERVICE" || true

echo "Keepalive loop started for docker compose service: $SERVICE"
while true; do
  if ! start_docker_if_needed; then
    sleep "$INTERVAL_SECONDS"
    continue
  fi

  cid="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    echo "Container is missing; recreating..."
    compose_up
    sleep "$INTERVAL_SECONDS"
    continue
  fi

  state="$(container_state "$cid" || true)"
  status="${state%% *}"
  health="${state#* }"

  case "$status:$health" in
    running:healthy|running:starting|running:no-healthcheck)
      ;;
    running:unhealthy)
      echo "Container is unhealthy; restarting $SERVICE..."
      docker compose restart "$SERVICE"
      docker compose logs --tail="$LOG_TAIL" "$SERVICE" || true
      ;;
    *)
      echo "Container state is $state; recreating $SERVICE..."
      compose_up
      docker compose logs --tail="$LOG_TAIL" "$SERVICE" || true
      ;;
  esac

  sleep "$INTERVAL_SECONDS"
done
