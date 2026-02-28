#!/bin/bash
set -e

REPO="https://github.com/crmcustoms/itcomms_migrations"
BRANCH="claude/review-migration-S7AZD"
DIR="/opt/itcomms"

echo "=== ITCOMMS Migration Server Setup ==="

# Clone or update repo
if [ -d "$DIR/.git" ]; then
  echo "→ Updating existing repo..."
  cd "$DIR" && git fetch && git checkout "$BRANCH" && git pull
else
  echo "→ Cloning repo..."
  git clone -b "$BRANCH" "$REPO" "$DIR"
  cd "$DIR"
fi

cd "$DIR"

# Create .env if not exists
if [ ! -f .env ]; then
  echo "→ Creating .env..."
  cat > .env <<EOF
PLANFIX_HOST=https://itcomms.planfix.com
PLANFIX_TOKEN=6ca06006655c6e695c495a4705609c85
MEGAPLAN_HOST=https://likhtman.megaplan.ru
MEGAPLAN_TOKEN=NzZkODNiOGUwMWNlMGIyMTY5NzlkMDkzOGEzOWFlOGI1MGYyNTk0YThmOWJkYWE5ZDFlMGMyNGU2YWQ2ZWI1ZA
API_SECRET=$(openssl rand -hex 16)
DRY_RUN=true
LOG_LEVEL=INFO
MEGAPLAN_DELAY=0.5
PLANFIX_DELAY=1.0
EOF
  echo "→ .env created (API_SECRET сгенерирован автоматически)"
else
  echo "→ .env уже существует, не трогаем"
fi

# Start
echo "→ Building and starting container..."
docker compose up -d --build

echo ""
echo "=== Ready! ==="
echo ""
echo "API_SECRET: $(grep API_SECRET .env | cut -d= -f2)"
echo "URL:        http://$(curl -s ifconfig.me):8000"
echo ""
echo "Health check:"
curl -s http://localhost:8000/health
echo ""
