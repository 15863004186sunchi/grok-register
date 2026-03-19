#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"

cd "$REPO_DIR"

# Pick compose command
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose not found. Install Docker Desktop or docker-compose." >&2
  exit 1
fi

# Ensure config.json exists
if [[ ! -f config.json ]]; then
  if [[ -f config.example.json ]]; then
    cp config.example.json config.json
    echo "Created config.json from config.example.json. Please edit it before running." >&2
  else
    echo "Missing config.example.json to create config.json." >&2
    exit 1
  fi
fi

mkdir -p sso logs

"${COMPOSE[@]}" up -d --build
"${COMPOSE[@]}" ps

echo "Deploy complete."