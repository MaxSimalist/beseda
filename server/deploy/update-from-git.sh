#!/usr/bin/env bash
# update-from-git.sh — обновление сервера «Беседа» из репозитория GitHub.
#
# Код (beseda_server/, requirements.txt, Dockerfile, bctl) берётся из репозитория,
# а локальные файлы сервера НЕ трогаются:
#   .env               — настройки (код регистрации, порты)
#   docker-compose.yml — может быть адаптирован под docker-compose v1
#   deploy/Caddyfile   — может быть адаптирован (on_demand_tls для работы по IP)
#   data/, backups/    — данные
#
# Установка на сервер (один раз):
#   cp server/deploy/update-from-git.sh ~/beseda/ && chmod +x ~/beseda/update-from-git.sh
# Дальше обновление — просто: ~/beseda/update-from-git.sh
set -euo pipefail

REPO_URL="https://github.com/MaxSimalist/beseda.git"
REPO_DIR="${BESEDA_REPO_DIR:-$HOME/beseda-repo}"
APP_DIR="${BESEDA_APP_DIR:-$HOME/beseda}"

if [ ! -d "$REPO_DIR/.git" ]; then
    echo "Клонирую репозиторий в $REPO_DIR..."
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
else
    echo "Обновляю репозиторий..."
    git -C "$REPO_DIR" pull --ff-only
fi

echo "Копирую код в $APP_DIR..."
rsync -a --delete --exclude '__pycache__' "$REPO_DIR/server/beseda_server/" "$APP_DIR/beseda_server/"
cp "$REPO_DIR/server/requirements.txt" "$REPO_DIR/server/Dockerfile" "$APP_DIR/"
cp "$REPO_DIR/server/bctl" "$APP_DIR/bctl" && chmod +x "$APP_DIR/bctl"

echo "Пересобираю и перезапускаю..."
cd "$APP_DIR"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
$DC up -d --build --remove-orphans
docker image prune -f >/dev/null || true

echo "Готово:"
docker ps --filter name=beseda --format '  {{.Names}}  {{.Status}}'
