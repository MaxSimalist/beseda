#!/usr/bin/env bash
# Установка (и обновление) сервера «Беседа» на Ubuntu 22.04 / 24.04.
#
#   sudo bash deploy/install.sh chat.example.ru you@mail.ru   # с доменом и HTTPS (рекомендуется)
#   sudo bash deploy/install.sh                               # без домена, по IP, без шифрования
#
# Повторный запуск = обновление кода с сохранением базы и настроек.
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
if [ "$(id -u)" != "0" ]; then echo "Запустите через sudo"; exit 1; fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx ufw >/dev/null

echo "==> Пользователь и каталоги"
id beseda &>/dev/null || useradd --system --home-dir /var/lib/beseda --create-home --shell /usr/sbin/nologin beseda
mkdir -p /opt/beseda /var/lib/beseda
chown beseda:beseda /var/lib/beseda
chmod 750 /var/lib/beseda
rm -rf /opt/beseda/beseda_server
cp -r "$SRC/beseda_server" "$SRC/requirements.txt" /opt/beseda/

echo "==> Python-окружение"
[ -d /opt/beseda/venv ] || python3 -m venv /opt/beseda/venv
/opt/beseda/venv/bin/pip install -q -U pip
/opt/beseda/venv/bin/pip install -q -r /opt/beseda/requirements.txt

echo "==> Настройки (/etc/beseda.env)"
if [ ! -f /etc/beseda.env ]; then
  CODE="$(python3 -c 'import secrets; print(secrets.token_urlsafe(8))')"
  sed "s/^BESEDA_REG_CODE=.*/BESEDA_REG_CODE=$CODE/" "$SRC/deploy/beseda.env" > /etc/beseda.env
  chgrp beseda /etc/beseda.env
  chmod 640 /etc/beseda.env
fi
UDP_PORT="$(grep -E '^BESEDA_UDP_PORT=' /etc/beseda.env | cut -d= -f2)"
UDP_PORT="${UDP_PORT:-50000}"
MAX_MB="$(grep -E '^BESEDA_MAX_UPLOAD_MB=' /etc/beseda.env | cut -d= -f2)"
MAX_MB="${MAX_MB:-50}"

echo "==> Служба systemd"
cp "$SRC/deploy/beseda.service" /etc/systemd/system/beseda.service
systemctl daemon-reload
systemctl enable beseda >/dev/null
systemctl restart beseda

echo "==> nginx"
sed -e "s/__DOMAIN__/${DOMAIN:-_}/" -e "s/__MAX_MB__/$((MAX_MB + 10))m/" \
    "$SRC/deploy/nginx-beseda.conf" > /etc/nginx/sites-available/beseda
ln -sf /etc/nginx/sites-available/beseda /etc/nginx/sites-enabled/beseda
if [ -z "$DOMAIN" ]; then rm -f /etc/nginx/sites-enabled/default; fi
nginx -t -q
systemctl reload nginx

echo "==> Брандмауэр"
SSH_PORT="$(ss -tlnp 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]; exit}')"
ufw allow "${SSH_PORT:-22}/tcp" >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow "${UDP_PORT}/udp" >/dev/null
ufw --force enable >/dev/null

if [ -n "$DOMAIN" ]; then
  echo "==> HTTPS-сертификат Let's Encrypt для $DOMAIN"
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  if [ -n "$EMAIL" ]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
  else
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect
  fi
  URL="https://$DOMAIN"
else
  IP="$(hostname -I | awk '{print $1}')"
  URL="http://$IP"
fi

sleep 1
CODE="$(grep -E '^BESEDA_REG_CODE=' /etc/beseda.env | cut -d= -f2)"
echo
echo "================================================================"
echo " Беседа запущена:  $(systemctl is-active beseda)"
echo " Адрес для клиента:   $URL"
echo " Код регистрации:     ${CODE:-(не требуется — регистрация открыта всем)}"
echo " Голос (UDP):         порт $UDP_PORT"
echo
echo " Первый зарегистрированный пользователь становится администратором."
echo " Логи:      journalctl -u beseda -f"
echo " Настройки: /etc/beseda.env  (после правки: systemctl restart beseda)"
echo " Данные:    /var/lib/beseda  (это и надо бэкапить)"
if [ -z "$DOMAIN" ]; then
  echo " ВНИМАНИЕ: без домена соединение не шифруется. Пароли идут открытым текстом."
fi
echo " Если у хостинга есть свой брандмауэр в панели — откройте там 80, 443/tcp и $UDP_PORT/udp."
echo "================================================================"
