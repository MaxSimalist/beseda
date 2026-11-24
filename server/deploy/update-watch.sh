#!/usr/bin/env bash
# update-watch.sh — наблюдатель автообновления сервера «Беседа» (запускается cron'ом).
#
# Если в админке нажали «Обновить сейчас», сервер кладёт файл /data/update_request
# (в Docker-томе). Этот скрипт видит его через docker exec и запускает update-from-git.sh.
# Результат пишется в /data/update_status.json — админка показывает его в настройках.
#
# Установка на сервере (один раз):
#   cp server/deploy/update-watch.sh ~/beseda/ && chmod +x ~/beseda/update-watch.sh
#   (crontab -l 2>/dev/null; echo '*/2 * * * * $HOME/beseda/update-watch.sh') | crontab -
set -uo pipefail

APP_DIR="${BESEDA_APP_DIR:-$HOME/beseda}"
CONTAINER="${BESEDA_CONTAINER:-beseda}"
LOG="$HOME/beseda-update.log"

docker exec "$CONTAINER" test -f /data/update_request 2>/dev/null || exit 0
docker exec "$CONTAINER" rm -f /data/update_request

write_status() {  # $1 — состояние (running/done/error), $2 — пояснение
    printf '{"state":"%s","at":%s,"detail":"%s"}\n' "$1" "$(date +%s)" "$2" \
        | docker exec -i "$CONTAINER" sh -c 'cat > /data/update_status.json' 2>/dev/null || true
}

echo "=== $(date '+%F %T') запрошено обновление из админки ===" >> "$LOG"
write_status running "идёт обновление"
if "$APP_DIR/update-from-git.sh" >> "$LOG" 2>&1; then
    # контейнер пересоздан — статус пишем уже в новый
    write_status done "обновлено успешно"
    echo "=== обновление завершено ===" >> "$LOG"
else
    write_status error "см. beseda-update.log на сервере"
    echo "=== ОШИБКА обновления ===" >> "$LOG"
fi
