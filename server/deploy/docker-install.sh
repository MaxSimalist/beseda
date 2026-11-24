#!/usr/bin/env bash
# Установка «Беседы» в Docker (Debian 12/13, Ubuntu). Запускать в папке server:
#
#   sudo bash deploy/docker-install.sh chat.example.ru     # с доменом → HTTPS (рекомендуется)
#   sudo bash deploy/docker-install.sh                     # без домена, по IP, без шифрования
#
# Повторный запуск = обновление: пересобирает образ, база и настройки (.env) сохраняются.
set -euo pipefail

DOMAIN="${1:-}"
cd "$(dirname "$0")/.."
chmod +x bctl 2>/dev/null || true   # при копировании с Windows флаг «исполняемый» теряется

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Docker ---
command -v docker >/dev/null || die "Docker не найден"
docker info >/dev/null 2>&1 || die "Нет доступа к Docker. Запустите через sudo или добавьте пользователя в группу docker"
docker compose version >/dev/null 2>&1 || die "Нет плагина docker compose. Установите: apt install docker-compose-plugin"

port_busy() {  # занят ли TCP-порт кем-то, кроме наших контейнеров
  local p="$1"
  docker ps --format '{{.Names}} {{.Ports}}' | grep -E "^beseda-caddy .*:$p->" >/dev/null && return 1
  if command -v ss >/dev/null; then
    ss -Hltn "sport = :$p" | grep -q .
  else  # без iproute2: пробуем подключиться
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null
  fi
}

# --- .env ---
if [ ! -f .env ]; then
  say "Создаю настройки (.env)"
  CODE="$(tr -dc 'a-km-z2-9' </dev/urandom | head -c 8 || true)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ -n "$DOMAIN" ]; then SITE="$DOMAIN"; URL="https://$DOMAIN"; else SITE=":80"; URL="http://${IP:-IP_СЕРВЕРА}"; fi
  cat > .env <<EOF
# Настройки «Беседы». После правки: docker compose up -d
# Домен для HTTPS или :80 (без домена)
BESEDA_SITE=$SITE
BESEDA_PUBLIC_URL=$URL
# Код для регистрации (пусто — без кода). Можно менять и в веб-админке.
BESEDA_REG_CODE=$CODE
BESEDA_UDP_PORT=50000
BESEDA_MAX_UPLOAD_MB=50
EOF
  chmod 600 .env
else
  say "Настройки .env уже есть — оставляю как есть"
  if [ -n "$DOMAIN" ] && ! grep -q "^BESEDA_SITE=$DOMAIN$" .env; then
    sed -i "s|^BESEDA_SITE=.*|BESEDA_SITE=$DOMAIN|; s|^BESEDA_PUBLIC_URL=.*|BESEDA_PUBLIC_URL=https://$DOMAIN|" .env
    say "Домен изменён на $DOMAIN"
  fi
fi
set -a; . ./.env; set +a

# --- проверки сети ---
if [ "${COMPOSE_FILE:-}" = "" ] && { port_busy 80 || port_busy 443; }; then
  warn "Порты 80/443 уже заняты другой программой:"
  command -v ss >/dev/null && ss -Hltnp 'sport = :80 or sport = :443' | sed 's/^/     /' || true
  warn "Встроенный Caddy запускать не буду. Сервер будет слушать 127.0.0.1:8765 — направьте туда свой прокси."
  echo "COMPOSE_FILE=docker-compose.yml:docker-compose.external-proxy.yml" >> .env
  export COMPOSE_FILE=docker-compose.yml:docker-compose.external-proxy.yml
fi
EXTERNAL=0; [ -n "${COMPOSE_FILE:-}" ] && EXTERNAL=1

if command -v ss >/dev/null && ss -Hlun "sport = :${BESEDA_UDP_PORT}" | grep -q . && ! docker ps --format '{{.Names}}' | grep -qx beseda; then
  die "UDP-порт ${BESEDA_UDP_PORT} занят. Поменяйте BESEDA_UDP_PORT в .env"
fi

if [ "$BESEDA_SITE" != ":80" ] && [ "$EXTERNAL" = 0 ]; then
  RES="$(getent ahostsv4 "$BESEDA_SITE" | awk '{print $1; exit}' || true)"
  if [ -z "$RES" ]; then
    warn "Домен $BESEDA_SITE пока не находится в DNS — сертификат не выпустится, пока DNS не обновится."
  elif ! hostname -I | tr ' ' '\n' | grep -qx "$RES"; then
    warn "Домен $BESEDA_SITE указывает на $RES, а у этого сервера адреса: $(hostname -I)"
    warn "Если сервер за NAT хостинга — это нормально, иначе исправьте A-запись домена."
  fi
fi

# --- брандмауэр (если включён) ---
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  say "Открываю порты в ufw"
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow "${BESEDA_UDP_PORT}/udp" >/dev/null
fi

# --- запуск ---
say "Собираю образ и запускаю контейнеры (первый раз 1–3 минуты)…"
docker compose up -d --build --remove-orphans

say "Жду, пока сервер ответит…"
for i in $(seq 1 40); do
  st="$(docker inspect -f '{{.State.Health.Status}}' beseda 2>/dev/null || echo starting)"
  [ "$st" = healthy ] && break
  [ "$st" = unhealthy ] && { docker compose logs --tail 40 beseda; die "Сервер не запустился, логи выше"; }
  sleep 2
done
[ "$st" = healthy ] || { docker compose logs --tail 40 beseda; die "Сервер не ответил за 80 секунд"; }

CODE="$(grep -E '^BESEDA_REG_CODE=' .env | cut -d= -f2-)"
echo
echo "================================================================"
echo " Беседа запущена в Docker"
echo " Адрес для клиента:  ${BESEDA_PUBLIC_URL}"
echo " Веб-админка:        ${BESEDA_PUBLIC_URL%/}/admin"
echo " Код регистрации:    ${CODE:-(не нужен)}"
echo " Голос:              UDP ${BESEDA_UDP_PORT}"
echo
echo " Первый зарегистрированный пользователь — администратор."
echo " Управление:  ./bctl logs | restart | update | backup | users | reset-password ЛОГИН"
if [ "$EXTERNAL" = 1 ]; then
  echo
  echo " Режим «свой прокси»: сервер на http://127.0.0.1:8765 — настройте на него свой"
  echo " прокси с WebSocket и большим лимитом тела запроса (пример — в README)."
elif [ "$BESEDA_SITE" != ":80" ]; then
  echo " Сертификат HTTPS Caddy получает сам в первые минуты; если адрес не открывается —"
  echo " смотрите: docker compose logs caddy"
else
  echo " ВНИМАНИЕ: без домена соединение не шифруется, пароли идут открытым текстом."
fi
echo " Если у хостинга есть брандмауэр в панели — откройте там 80, 443/tcp и ${BESEDA_UDP_PORT}/udp."
echo "================================================================"
