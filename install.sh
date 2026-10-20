#!/bin/sh
set -eu

# One-command installer for the ai-ch updater on a ZimaOS host.
# Run as root; this script installs no Docker image and does not rebuild one.

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти установщик от root (например, через sudo)." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "На этом окружении не найден systemctl. Установщик рассчитан на ZimaOS-хост с systemd." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Не найдена команда docker. Установщик должен запускаться на хосте ZimaOS, где доступен Docker." >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "Не найдена команда curl. Установи curl либо загрузи файлы репозитория вручную." >&2
  exit 1
fi

INSTALL_DIR=/DATA/AppData/ai-ch-updater
RAW_BASE=https://raw.githubusercontent.com/vwxcc/update/main
mkdir -p "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"

echo "Загружаю обновлятор из GitHub..."
curl --fail --silent --show-error --location "$RAW_BASE/auto_deploy_daemon.py" -o "$INSTALL_DIR/auto_deploy_daemon.py"
chmod 755 "$INSTALL_DIR/auto_deploy_daemon.py"

container_name=""
health_port=""
health_url=""
if [ -f /etc/ai-ch-updater.env ]; then
  echo "Найдена существующая конфигурация — сохраняю имя контейнера и адрес health-check."
  container_name=$(sed -n 's/^DOCKER_CONTAINER=//p' /etc/ai-ch-updater.env | head -n 1)
  health_url=$(sed -n 's/^HEALTH_URL=//p' /etc/ai-ch-updater.env | head -n 1)
  if [ -z "$container_name" ]; then
    echo "В /etc/ai-ch-updater.env не задан DOCKER_CONTAINER." >&2
    exit 1
  fi
  health_port=$(printf '%s' "$health_url" | sed -n 's#^http://127[.]0[.]0[.]1:\([0-9][0-9]*\)/.*#\1#p')
  if [ -z "$health_port" ]; then
    health_port=3050
  fi
else
  printf "Имя существующего контейнера ai-ch [chatstudio-v2]: "
  read -r container_name || true
  if [ -z "$container_name" ]; then
    container_name=chatstudio-v2
  fi
  case "$container_name" in
    *[!A-Za-z0-9_.-]*)
      echo "Недопустимое имя контейнера: используйте латинские буквы, цифры, точку, дефис или подчёркивание." >&2
      exit 1
      ;;
  esac

  printf "Порт приложения на хосте для health-check [3050]: "
  read -r health_port || true
  if [ -z "$health_port" ]; then
    health_port=3050
  fi
  case "$health_port" in
    *[!0-9]*|"")
      echo "Порт должен быть числом." >&2
      exit 1
      ;;
  esac
  if [ "$health_port" -lt 1 ] || [ "$health_port" -gt 65535 ]; then
    echo "Порт должен быть в диапазоне 1–65535." >&2
    exit 1
  fi
fi

if ! docker inspect "$container_name" >/dev/null 2>&1; then
  echo "Контейнер «$container_name» не найден. Проверь имя и повтори установку." >&2
  exit 1
fi
if [ "$(docker inspect --format='{{.State.Running}}' "$container_name" 2>/dev/null || true)" != "true" ]; then
  echo "Контейнер «$container_name» сейчас не запущен. Запусти его в ZimaOS и повтори установку." >&2
  exit 1
fi
if [ ! -f /etc/ai-ch-updater.env ]; then
  cat > /etc/ai-ch-updater.env <<EOF
GITHUB_REPO=vwxcc/deepseekw
GITHUB_BRANCH=main
TARGET_MODE=docker
DOCKER_CONTAINER=$container_name
APP_DIR=/app
HEALTH_URL=http://127.0.0.1:$health_port/api/health
HEALTH_TIMEOUT=90
HTTP_TIMEOUT=25
KEEP_RELEASES=3
UPDATE_ROOT=$INSTALL_DIR
EOF
  chmod 600 /etc/ai-ch-updater.env
else
  echo "Файл /etc/ai-ch-updater.env уже существует — сохраняю его настройки."
fi

cat > /etc/systemd/system/ai-ch-updater.service <<'EOF'
[Unit]
Description=ai-ch GitHub updater (staged update with health-check rollback)
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
EnvironmentFile=/etc/ai-ch-updater.env
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/DATA/AppData/ai-ch-updater
ExecStart=/usr/bin/python3 /DATA/AppData/ai-ch-updater/auto_deploy_daemon.py
TimeoutStartSec=20min
Nice=10
EOF

cat > /etc/systemd/system/ai-ch-updater.timer <<'EOF'
[Unit]
Description=Check GitHub for ai-ch updates every 3 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
AccuracySec=15s
Persistent=true
Unit=ai-ch-updater.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now ai-ch-updater.timer

echo
echo "Установлено."
echo "Контейнер: $container_name"
echo "Health-check: http://127.0.0.1:$health_port/api/health"
echo "Проверка GitHub запланирована каждые 3 минуты."
echo "Первый запуск таймера может установить актуальный main и перезапустить контейнер."
echo "Состояние: $INSTALL_DIR/state.json"
echo "Журнал: $INSTALL_DIR/logs/updater.log"
echo "Статус: systemctl status ai-ch-updater.timer"
echo "Ручная проверка: systemctl start ai-ch-updater.service"
