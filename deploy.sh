#!/usr/bin/env bash
# Выкладка на сервер. Конфигурацию и данные не трогаем — они живут только там.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@1.2.3.4}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/max-digest-bot}"
DIR=/opt/max-digest-bot

rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=no" \
  --exclude '.venv' --exclude 'data' --exclude 'backups' \
  --exclude '.env' --exclude '__pycache__' --exclude '.git' \
  "$(dirname "$0")/" "$HOST:$DIR/"

ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST" \
  "cd $DIR && ./.venv/bin/pip install -q -r requirements.txt && sudo systemctl restart max-digest-bot && sleep 5 && systemctl is-active max-digest-bot"
