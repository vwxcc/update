#!/bin/sh
set -eu
umask 077

# Installer and updater bootstrap for the current AI CH application on ZimaOS.
# Existing containers are detected and preserved; no image build/buildx is used.

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти установщик от root: sudo sh /tmp/ai-ch-updater-install.sh" >&2
  exit 1
fi

for cmd in docker curl tar python3 systemctl; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Не найдена команда $cmd. Нужны Docker, curl, tar, Python 3 и systemd." >&2
    exit 1
  }
done

APP_DATA_ROOT=/DATA/AppData
UPDATE_ROOT=$APP_DATA_ROOT/ai-ch-updater
REPO=vwxcc/deepseekw
BRANCH=main
RAW_BASE=https://raw.githubusercontent.com/vwxcc/update/main
CONTAINER_PORT=8000
mkdir -p "$UPDATE_ROOT" "$UPDATE_ROOT/logs"
chmod 700 "$UPDATE_ROOT"

CURL_CONFIG=""
cleanup() {
  [ -z "$CURL_CONFIG" ] || rm -f -- "$CURL_CONFIG"
  [ -z "${ARCHIVE:-}" ] || rm -f -- "$ARCHIVE"
  [ -z "${HEADERS_FILE:-}" ] || rm -f -- "$HEADERS_FILE"
  [ -z "${BODY_FILE:-}" ] || rm -f -- "$BODY_FILE"
}
trap cleanup EXIT HUP INT TERM

# Reuse the host-only token during updater reinstall. Never store it in the app.
GITHUB_TOKEN=${GITHUB_TOKEN:-}
if [ -z "$GITHUB_TOKEN" ] && [ -r /etc/ai-ch-updater.env ]; then
  GITHUB_TOKEN=$(sed -n 's/^GITHUB_TOKEN=//p' /etc/ai-ch-updater.env | head -n 1 | tr -d '\r')
fi
if [ -z "$GITHUB_TOKEN" ]; then
  echo
  echo "Для приватного репозитория vwxcc/deepseekw нужен GitHub fine-grained token."
  echo "Права: только этот репозиторий, Contents: Read-only."
  printf "GitHub token (ввод скрыт): "
  STTY_STATE=$(stty -g 2>/dev/null || true)
  stty -echo 2>/dev/null || true
  read -r GITHUB_TOKEN || true
  [ -z "$STTY_STATE" ] || stty "$STTY_STATE" 2>/dev/null || true
  printf "\n"
fi
case "$GITHUB_TOKEN" in
  ""|*[!A-Za-z0-9_]*)
    echo "Токен пустой или содержит неподдерживаемые символы." >&2
    exit 1
    ;;
esac

CURL_CONFIG=$(mktemp "$UPDATE_ROOT/.github-curl.XXXXXX")
chmod 600 "$CURL_CONFIG"
cat > "$CURL_CONFIG" <<EOF
header = "Authorization: Bearer $GITHUB_TOKEN"
header = "Accept: application/vnd.github+json"
header = "X-GitHub-Api-Version: 2022-11-28"
EOF

META_FILE=$UPDATE_ROOT/.github-commit.json
echo "Проверяю доступ к приватному репозиторию $REPO/$BRANCH..."
if ! curl --config "$CURL_CONFIG" --fail --silent --show-error --connect-timeout 15 \
    --max-time 35 "https://api.github.com/repos/$REPO/commits/$BRANCH" -o "$META_FILE"; then
  rm -f "$META_FILE"
  echo "GitHub не предоставил доступ к репозиторию. Проверь token и право Contents: Read-only." >&2
  exit 1
fi
commit_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["sha"])' "$META_FILE")
case "$commit_sha" in
  *[!0-9a-fA-F]*|"") echo "GitHub вернул некорректный SHA." >&2; exit 1 ;;
esac
[ "${#commit_sha}" -eq 40 ] || {
  echo "GitHub вернул SHA неверной длины." >&2
  exit 1
}

# Find a live AI CH container by its actual application structure.
detect_existing_container() {
  for candidate in $(docker ps --format '{{.Names}}'); do
    case "$candidate" in *[!A-Za-z0-9_.-]*) continue ;; esac
    if docker exec "$candidate" test -f /app/app/main.py >/dev/null 2>&1 &&
       docker exec "$candidate" test -f /app/frontend/index.html >/dev/null 2>&1 &&
       docker exec "$candidate" test -f /app/requirements.txt >/dev/null 2>&1; then
      printf "%s\n" "$candidate"
      return 0
    fi
  done
  return 0
}

detected_container=$(detect_existing_container)
default_container=$detected_container
[ -n "$default_container" ] || default_container=${DOCKER_CONTAINER:-ai-ch-mega}
printf "Контейнер AI CH [%s]: " "$default_container"
read -r container_name || true
[ -n "$container_name" ] || container_name=$default_container
case "$container_name" in
  *[!A-Za-z0-9_.-]*|"")
    echo "Недопустимое имя контейнера." >&2
    exit 1
    ;;
esac

container_exists=false
if docker inspect "$container_name" >/dev/null 2>&1; then
  container_exists=true
fi

detect_published_port() {
  docker inspect --format '{{json .NetworkSettings.Ports}}' "$container_name" 2>/dev/null |
    python3 -c 'import json,sys; d=json.load(sys.stdin); a=d.get("8000/tcp") or []; print(a[0].get("HostPort","") if a else "")' 2>/dev/null || true
}

if [ "$container_exists" = "true" ]; then
  running=$(docker inspect --format='{{.State.Running}}' "$container_name")
  if [ "$running" != "true" ]; then
    echo "Контейнер $container_name существует, но остановлен. Запускаю его без пересоздания..."
    docker start "$container_name" >/dev/null
  fi
  if ! docker exec "$container_name" test -f /app/app/main.py ||
     ! docker exec "$container_name" test -f /app/frontend/index.html ||
     ! docker exec "$container_name" test -f /app/requirements.txt; then
    echo "В контейнере $container_name не найдена актуальная структура /app/app, /app/frontend, requirements.txt." >&2
    echo "Контейнер и данные не изменены. Проверь выбранное имя." >&2
    exit 1
  fi
  detected_port=$(detect_published_port)
  if [ -n "$detected_port" ]; then
    health_port=$detected_port
    echo "У контейнера $container_name уже опубликован порт $health_port; не меняю его."
  else
    default_port=3050
    printf "На хосте не найден проброс 8000/tcp. Порт для ссылки [%s]: " "$default_port"
    read -r health_port || true
    [ -n "$health_port" ] || health_port=$default_port
  fi
  echo "Найден существующий контейнер $container_name: сохраняю его, .env, mounts, data и uploads."
else
  default_port=3050
  printf "Внешний порт для нового контейнера [%s]: " "$default_port"
  read -r health_port || true
  [ -n "$health_port" ] || health_port=$default_port
fi

case "$health_port" in *[!0-9]*|"") echo "Порт должен быть числом." >&2; exit 1 ;; esac
if [ "$health_port" -lt 1 ] || [ "$health_port" -gt 65535 ]; then
  echo "Порт должен быть в диапазоне 1–65535." >&2
  exit 1
fi
HEALTH_URL="http://127.0.0.1:$health_port/api/health"

download_private_archive() {
  api_url=$1
  output_file=$2
  HEADERS_FILE=$(mktemp "$UPDATE_ROOT/.archive-headers.XXXXXX")
  BODY_FILE=$(mktemp "$UPDATE_ROOT/.archive-body.XXXXXX")
  if ! curl --config "$CURL_CONFIG" --silent --show-error --connect-timeout 15 \
      --max-time 35 --dump-header "$HEADERS_FILE" --output "$BODY_FILE" "$api_url" 2>/dev/null; then
    echo "Не удалось запросить архив приватного репозитория GitHub." >&2
    return 1
  fi
  status=$(awk '/^HTTP\// { code=$2 } END { print code }' "$HEADERS_FILE")
  case "$status" in
    200)
      mv "$BODY_FILE" "$output_file"
      ;;
    301|302|303|307|308)
      location=$(sed -n 's/^[Ll]ocation:[[:space:]]*//p' "$HEADERS_FILE" | tr -d '\r' | tail -n 1)
      case "$location" in
        https://codeload.github.com/*) ;;
        *)
          echo "GitHub вернул неожиданный адрес архива; скачивание остановлено." >&2
          return 1
          ;;
      esac
      # The signed codeload URL is fetched without forwarding the private token.
      if ! curl --fail --silent --show-error --location --retry 3 \
          --connect-timeout 15 --max-time 300 "$location" --output "$output_file" 2>/dev/null; then
        rm -f "$output_file"
        echo "Не удалось скачать архив по временному адресу GitHub." >&2
        return 1
      fi
      ;;
    *)
      echo "GitHub вернул неожиданный ответ при скачивании архива (HTTP ${status:-unknown})." >&2
      return 1
      ;;
  esac
  [ -s "$output_file" ] || {
    rm -f "$output_file"
    echo "GitHub вернул пустой архив." >&2
    return 1
  }
  rm -f "$HEADERS_FILE" "$BODY_FILE"
  HEADERS_FILE=""
  BODY_FILE=""
}

if [ "$container_exists" = "false" ]; then
  APP_ROOT=$APP_DATA_ROOT/$container_name
  mkdir -p "$APP_ROOT" "$APP_ROOT/data" "$APP_ROOT/uploads"
  chmod 755 "$APP_ROOT" "$APP_ROOT/data" "$APP_ROOT/uploads"

  if [ ! -f "$APP_ROOT/.env" ]; then
    echo
    echo "Первичная настройка администратора AI CH"
    printf "Email администратора: "
    read -r admin_email || true
    case "$admin_email" in
      ""|*[!A-Za-z0-9_.@+-]*)
        echo "Email пустой или содержит неподдерживаемые символы." >&2
        exit 1
        ;;
    esac
    printf "Пароль администратора (не менее 8 символов): "
    STTY_STATE=$(stty -g 2>/dev/null || true)
    stty -echo 2>/dev/null || true
    read -r admin_password || true
    [ -z "$STTY_STATE" ] || stty "$STTY_STATE" 2>/dev/null || true
    printf "\n"
    if [ "${#admin_password}" -lt 8 ]; then
      echo "Пароль должен содержать не менее 8 символов." >&2
      exit 1
    fi
    case "$admin_password" in
      *[!A-Za-z0-9._@%+-]*)
        echo "Используй в пароле только латинские буквы, цифры и . _ @ % + -." >&2
        exit 1
        ;;
    esac
    session_secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
    cat > "$APP_ROOT/.env" <<EOF
APP_NAME=AI CH
SESSION_SECRET=$session_secret
ADMIN_EMAIL=$admin_email
ADMIN_PASSWORD=$admin_password
RUN_SANDBOX_ENABLED=0
EOF
    chmod 600 "$APP_ROOT/.env"
  fi

  echo "Скачиваю коммит $commit_sha для первоначальной установки..."
  STAGE=$UPDATE_ROOT/initial-install
  ARCHIVE=$UPDATE_ROOT/.ai-ch-source.tar.gz
  rm -rf "$STAGE"
  mkdir -p "$STAGE"
  download_private_archive "https://api.github.com/repos/$REPO/tarball/$commit_sha" "$ARCHIVE"
  tar -xzf "$ARCHIVE" -C "$STAGE"
  SOURCE_DIR=$(find "$STAGE" -mindepth 1 -maxdepth 1 -type d | head -n 1)
  if [ -z "$SOURCE_DIR" ] || [ ! -f "$SOURCE_DIR/app/main.py" ] ||
     [ ! -f "$SOURCE_DIR/frontend/index.html" ] || [ ! -f "$SOURCE_DIR/requirements.txt" ]; then
    echo "В архиве отсутствуют app/main.py, frontend/index.html или requirements.txt." >&2
    exit 1
  fi
  python3 - "$SOURCE_DIR" <<'PY'
import ast
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
files = list((root / "app").rglob("*.py"))
if not files:
    raise SystemExit("В архиве нет Python-файлов приложения.")
for path in files:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
if not (root / "requirements.txt").read_text(encoding="utf-8").strip():
    raise SystemExit("requirements.txt пуст.")
print(f"Проверен синтаксис {len(files)} Python-файлов.")
PY

  echo "Скачиваю базовый образ python:3.12-slim..."
  docker pull python:3.12-slim
  docker create --name "$container_name" --restart unless-stopped \
    -p "$health_port:8000" \
    --env-file "$APP_ROOT/.env" \
    -v "$APP_ROOT/data:/app/data" \
    -v "$APP_ROOT/uploads:/app/uploads" \
    -w /app \
    python:3.12-slim \
    sh -c 'if [ ! -f /app/.deps-installed ]; then python -m pip install --no-cache-dir -r /app/requirements.txt && touch /app/.deps-installed; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8000' >/dev/null
  docker cp "$SOURCE_DIR/app" "$container_name:/app/"
  docker cp "$SOURCE_DIR/frontend" "$container_name:/app/"
  docker cp "$SOURCE_DIR/requirements.txt" "$container_name:/app/requirements.txt"
  rm -rf "$STAGE"
  docker start "$container_name" >/dev/null

  healthy=false
  attempts=0
  while [ "$attempts" -lt 60 ]; do
    if docker exec "$container_name" python -c \
      'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health",timeout=3)' \
      >/dev/null 2>&1; then
      healthy=true
      break
    fi
    attempts=$((attempts + 1))
    sleep 3
  done
  if [ "$healthy" != "true" ]; then
    echo "AI CH не прошёл внутренний health-check. Контейнер оставлен для диагностики." >&2
    echo "Проверь: docker logs --tail 150 $container_name" >&2
    exit 1
  fi

  python3 - "$UPDATE_ROOT/state.json" "$commit_sha" <<'PY'
import json,sys
from datetime import datetime,timezone
path,sha=sys.argv[1:]
now=datetime.now(timezone.utc).isoformat(timespec="seconds")
with open(path,"w",encoding="utf-8") as f:
    json.dump({"remote_sha":sha,"applied_sha":sha,"last_check":now,"last_success":now,
               "last_error":None,"failed_sha":None,"failed_updater_version":None,
               "dependency_sha":None,"dependency_history":[]},f,ensure_ascii=False,indent=2)
    f.write("\n")
PY
fi

docker update --restart unless-stopped "$container_name" >/dev/null

# Recover exactly the selected existing container after the Docker daemon starts.
cat > "$UPDATE_ROOT/recover_container.sh" <<'RECOVERY'
#!/bin/sh
set -eu
CONTAINER="${DOCKER_CONTAINER:-ai-ch-mega}"
command -v docker >/dev/null 2>&1 || {
  echo "Docker CLI не найден." >&2
  exit 1
}
docker inspect "$CONTAINER" >/dev/null 2>&1 || {
  echo "Контейнер $CONTAINER не найден; восстановление пропущено." >&2
  exit 0
}
running=$(docker inspect --format='{{.State.Running}}' "$CONTAINER")
if [ "$running" != "true" ]; then
  echo "Запускаю контейнер $CONTAINER после старта Docker."
  docker start "$CONTAINER" >/dev/null
else
  echo "Контейнер $CONTAINER уже запущен."
fi
RECOVERY
chmod 755 "$UPDATE_ROOT/recover_container.sh"

curl --fail --silent --show-error --location "$RAW_BASE/auto_deploy_daemon.py" \
  -o "$UPDATE_ROOT/auto_deploy_daemon.py"
chmod 755 "$UPDATE_ROOT/auto_deploy_daemon.py"

ENV_TMP=$(mktemp /etc/.ai-ch-updater.env.XXXXXX)
cat > "$ENV_TMP" <<EOF
GITHUB_REPO=$REPO
GITHUB_BRANCH=$BRANCH
GITHUB_TOKEN=$GITHUB_TOKEN
TARGET_MODE=docker
DOCKER_CONTAINER=$container_name
APP_DIR=/app
HEALTH_URL=$HEALTH_URL
CONTAINER_PORT=$CONTAINER_PORT
HEALTH_TIMEOUT=120
HTTP_TIMEOUT=25
KEEP_RELEASES=3
UPDATE_ROOT=$UPDATE_ROOT
EOF
chmod 600 "$ENV_TMP"
mv "$ENV_TMP" /etc/ai-ch-updater.env
rm -f "$META_FILE"

cat > /etc/systemd/system/ai-ch-recovery.service <<EOF
[Unit]
Description=Ensure AI CH container starts after ZimaOS boot
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
EnvironmentFile=/etc/ai-ch-updater.env
ExecStart=$UPDATE_ROOT/recover_container.sh
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/ai-ch-updater.service <<EOF
[Unit]
Description=AI CH GitHub updater (staged update with health-check rollback)
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
EnvironmentFile=/etc/ai-ch-updater.env
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$UPDATE_ROOT
ExecStart=/usr/bin/python3 $UPDATE_ROOT/auto_deploy_daemon.py
TimeoutStartSec=20min
Nice=10
EOF

cat > /etc/systemd/system/ai-ch-updater.timer <<'TIMER'
[Unit]
Description=Check GitHub for AI CH updates every 3 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
AccuracySec=15s
Persistent=true
Unit=ai-ch-updater.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable ai-ch-recovery.service
systemctl start ai-ch-recovery.service
systemctl enable --now ai-ch-updater.timer

echo
echo "Updater установлен."
echo "Репозиторий: $REPO/$BRANCH"
echo "Контейнер: $container_name"
echo "Внутренний порт приложения: $CONTAINER_PORT"
echo "Внешний адрес: http://<IP-ZimaOS>:$health_port/"
echo "Существующий контейнер, .env, data и uploads не пересоздавались."
echo "Проверка updater: systemctl start ai-ch-updater.service"
echo "Таймер: systemctl status ai-ch-updater.timer"
echo "Логи: journalctl -u ai-ch-updater.service -n 100 --no-pager"
echo "Файл лога: $UPDATE_ROOT/logs/updater.log"

if systemctl start ai-ch-updater.service; then
  echo "Ручная проверка updater завершилась успешно."
else
  echo "Updater настроен, но текущая проверка обновления завершилась с ошибкой." >&2
  echo "Проверь: journalctl -u ai-ch-updater.service -n 100 --no-pager" >&2
fi
