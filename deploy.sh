#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Pulling latest changes..."
git pull

echo "==> Stopping and removing containers..."
docker compose down

echo "==> Rebuilding and starting containers..."
docker compose up --build -d

echo "==> Waiting for containers to stabilise..."
sleep 5

echo "==> Container status:"
docker compose ps

echo ""
echo "Deploy complete."
