#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: DEPLOY_USER=root $0 <server-ip-or-host>" >&2
  exit 64
fi

HOST="$1"
DEPLOY_USER="${DEPLOY_USER:-root}"
APP_DIR="${APP_DIR:-/opt/telegram-bot-mvp}"
SSH_TARGET="${DEPLOY_USER}@${HOST}"
SSH_OPTS=(
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=4
)
RSYNC_SSH="ssh ${SSH_OPTS[*]}"

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo ".env is required for deployment" >&2
  exit 66
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required on the local machine" >&2
  exit 69
fi

ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "APP_DIR='$APP_DIR' bash -s" <<'REMOTE'
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true >/dev/null 2>&1; then
    echo "Deploy user must have passwordless sudo or be root." >&2
    exit 77
  fi
fi

if ! command -v sudo >/dev/null 2>&1; then
  apt-get update
  apt-get install -y sudo
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
  fi
  . /etc/os-release
  CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME}}"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
fi

sudo mkdir -p "$APP_DIR/data"
sudo chown -R "$USER:$USER" "$APP_DIR"
REMOTE

rsync -az --delete \
  -e "$RSYNC_SSH" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'bot.log' \
  --exclude '*.pyc' \
  --exclude 'data/' \
  ./ "$SSH_TARGET:$APP_DIR/"

ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "APP_DIR='$APP_DIR' bash -s" <<'REMOTE'
set -euo pipefail
cd "$APP_DIR"
chmod 600 .env
mkdir -p data
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs --tail=80 bot
REMOTE
