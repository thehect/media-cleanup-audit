#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mediacleanup}"
REPO_URL="${1:-${MEDIACLEANUP_REPO:-}}"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/media}"
PORT="${MEDIACLEANUP_PORT:-6996}"

if [[ -z "$REPO_URL" ]]; then
  echo "Usage: $0 https://github.com/YOUR_USER/YOUR_REPO.git"
  echo
  echo "Optional environment:"
  echo "  APP_DIR=/opt/mediacleanup"
  echo "  MEDIA_ROOT=/path/to/your/nas/mount"
  echo "  MEDIACLEANUP_PORT=6996"
  exit 1
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd git
require_cmd docker

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Missing Docker Compose. Install the Docker Compose plugin or docker-compose."
  exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Cloning Media Cleanup into $APP_DIR"
  if [[ ! -w "$(dirname "$APP_DIR")" ]]; then
    sudo mkdir -p "$APP_DIR"
    sudo chown "$USER":"$USER" "$APP_DIR"
  else
    mkdir -p "$APP_DIR"
  fi
  git clone "$REPO_URL" "$APP_DIR"
else
  echo "Updating Media Cleanup in $APP_DIR"
  git -C "$APP_DIR" pull --ff-only
fi

cd "$APP_DIR"

mkdir -p reports

if [[ ! -f config.yml ]]; then
  cp config.example.yml config.yml
  echo
  echo "Created $APP_DIR/config.yml"
  echo "Edit it with your API keys and paths, then rerun this script."
  echo
  echo "Suggested next command:"
  echo "  nano $APP_DIR/config.yml"
  exit 0
fi

export MEDIA_ROOT
export MEDIACLEANUP_PORT="$PORT"

echo "Building and starting Media Cleanup dashboard..."
"${COMPOSE[@]}" up -d --build mediacleanup

echo
echo "Media Cleanup is starting."
echo "Open: http://MEDIACLEANUP:$PORT"
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
if [[ -n "$SERVER_IP" ]]; then
  echo "Or:   http://$SERVER_IP:$PORT"
fi
echo
echo "Reports folder: $APP_DIR/reports"
