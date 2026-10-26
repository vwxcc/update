#!/bin/sh
set -eu
umask 077

# Fresh deployment + automatic updater for ai-ch on a ZimaOS host.
# Uses python:3.12-slim and docker cp; no Dockerfile build/buildx required.

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти установщик от root: sudo sh /tmp/ai-ch-updater-install.sh" >&2
  exit 1
fi

for cmd in docker curl tar python3 systemctl; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Не найдена команда $cmd. Установщик рассчитан на хост ZimaOS с Docker, curl, tar, Python 3 и systemd." >&2
    exit 1
  }
done

APP_ROOT=/DATA/AppData/ai-ch
UPDATE_ROOT=/DATA/AppData/ai-ch-updater
REPO=vwxcc/deepseekw
BRANCH=main
RAW_BASE=https://raw.githubusercontent.com/vwxcc/update/main
mkdir -p "$APP_ROOT" "$APP_ROOT/data" "$APP_ROOT/uploads" "$UPDATE_ROOT"
chmod 755 "$APP_ROOT" "$APP_ROOT/data" "$APP_ROOT/uploads" "$UPDATE_ROOT"

CURL_CONFIG=""
cleanup_curl_config() {
  [ -z "$CURL_CONFIG" ] || rm -f -- "$CURL_CONFIG"
}
trap cleanup_curl_config EXIT HUP INT TERM

# Download the GitHub archive through the authenticated API endpoint. The API
# returns a short-lived codeload URL; fetch that second URL without credentials.
download_private_archive() {
  api_url=$1
  output_file=$2
  headers_file=$(mktemp "$UPDATE_ROOT/.archive-headers.XXXXXX")
  body_file=$(mktemp "$UPDATE_ROOT/.archive-body.XXXXXX")
  if ! curl --config "$CURL_CONFIG" --silent --show-error --connect-timeout 15 \
      --max-time 35 --dump-header "$headers_file" --output "$body_file" "$api_url" 2>/dev/null; then
    rm -f "$headers_file" "$body_file"
    echo "Не удалось запросить архив приватного репозитория GitHub." >&2
    return 1
  fi
  status=$(awk '/^HTTP\// { code=$2 } END { print code }' "$headers_file")
  case "$status" in
    200)
      mv "$body_file" "$output_file"
      ;;
    301|302|303|307|308)
      location=$(sed -n 's/^[Ll]ocation:[[:space:]]*//p' "$headers_file" | tr -d '\r' | tail -n 1)
      case "$location" in
        https://codeload.github.com/*) ;;
        *)
          rm -f "$headers_file" "$body_file"
          echo "GitHub вернул неожиданный адрес архива; скачивание остановлено." >&2
          return 1
          ;;
      esac
      # The redirect URL is temporary and signed. Do not print or persist it,
      # and never send the PAT to codeload.
      if ! curl --fail --silent --show-error --location --retry 3 \
          --connect-timeout 15 --max-time 300 "$location" --output "$output_file" 2>/dev/null; then
        rm -f "$headers_file" "$body_file" "$output_file"
        echo "Не удалось скачать архив по временному адресу GitHub." >&2
        return 1
      fi
      rm -f "$body_file"
      ;;
    *)
      rm -f "$headers_file" "$body_file"
      echo "GitHub не вернул ожидаемый архив или перенаправление (HTTP ${status:-unknown}). Проверь токен и права Contents: Read-only." >&2
      return 1
      ;;
  esac
  rm -f "$headers_file"
  [ -s "$output_file" ] || {
    rm -f "$output_file"
    echo "GitHub вернул пустой архив." >&2
    return 1
  }
}

# Reuse the existing host-only token on upgrades. Never store it in the app .env.
GITHUB_TOKEN=${GITHUB_TOKEN:-}
if [ -z "$GITHUB_TOKEN" ] && [ -r /etc/ai-ch-updater.env ]; then
  GITHUB_TOKEN=$(sed -n 's/^GITHUB_TOKEN=//p' /etc/ai-ch-updater.env | head -n 1 | tr -d '\r')
fi
if [ -z "$GITHUB_TOKEN" ]; then
  echo
  echo "Для скачивания приватного vwxcc/deepseekw нужен GitHub fine-grained token."
  echo "Разрешения: только репозиторий vwxcc/deepseekw, Contents: Read-only."
  printf "GitHub token (ввод скрыт): "
  stty -echo 2>/dev/null || true
  read -r GITHUB_TOKEN || true
  stty echo 2>/dev/null || true
  printf "\n"
fi
case "$GITHUB_TOKEN" in
  ""|*[!A-Za-z0-9_]*)
    echo "Токен пустой или содержит неподдерживаемые символы; разрешены только латинские буквы, цифры и подчёркивание." >&2
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

printf "Имя контейнера ai-ch [ai-ch]: "
read -r container_name || true
[ -n "$container_name" ] || container_name=ai-ch
case "$container_name" in
  *[!A-Za-z0-9_.-]*)
    echo "Недопустимое имя контейнера." >&2
    exit 1
    ;;
esac

printf "Порт ai-ch на хосте [3050]: "
read -r health_port || true
[ -n "$health_port" ] || health_port=3050
case "$health_port" in
  *[!0-9]*|"") echo "Порт должен быть числом." >&2; exit 1 ;;
esac
if [ "$health_port" -lt 1 ] || [ "$health_port" -gt 65535 ]; then
  echo "Порт должен быть в диапазоне 1–65535." >&2
  exit 1
fi
HEALTH_URL="http://127.0.0.1:$health_port/api/health"

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
  printf "Пароль администратора (минимум 8 символов; допустимы буквы, цифры и . _ @ %% + -): "
  stty -echo 2>/dev/null || true
  read -r admin_password || true
  stty echo 2>/dev/null || true
  printf "\n"
  if [ "${#admin_password}" -lt 8 ]; then
    echo "Пароль должен содержать не менее 8 символов." >&2
    exit 1
  fi
  case "$admin_password" in
    *[!A-Za-z0-9._@%+-]*)
      echo "Для надёжной записи в Docker env-файл используй в пароле только буквы, цифры и . _ @ % + -." >&2
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
  echo "Создан защищённый файл настроек: $APP_ROOT/.env"
else
  echo "Найден $APP_ROOT/.env — сохраняю настройки и секреты без изменений."
fi

container_exists=false
if docker inspect "$container_name" >/dev/null 2>&1; then
  container_exists=true
fi

# Validate credentials before changing the service or installing the timer.
META_FILE="$UPDATE_ROOT/.github-commit.json"
echo "Проверяю доступ к приватному репозиторию $REPO/$BRANCH..."
if ! curl --config "$CURL_CONFIG" --fail --silent --show-error --connect-timeout 15 \
    --max-time 35 "https://api.github.com/repos/$REPO/commits/$BRANCH" -o "$META_FILE"; then
  rm -f "$META_FILE"
  echo "Не удалось прочитать приватный репозиторий. Проверь token, его срок действия и Contents: Read-only для vwxcc/deepseekw." >&2
  exit 1
fi
commit_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sha"])' "$META_FILE")
case "$commit_sha" in
  ????????*) ;;
  *) echo "GitHub не вернул SHA коммита." >&2; exit 1 ;;
esac
case "$commit_sha" in
  *[!0-9a-fA-F]*) echo "GitHub вернул некорректный SHA." >&2; exit 1 ;;
esac
if [ "${#commit_sha}" -ne 40 ]; then
  echo "GitHub вернул SHA неверной длины." >&2
  exit 1
fi

if [ "$container_exists" = "false" ]; then
  echo
  echo "Скачиваю фиксированный коммит $REPO/$BRANCH ($commit_sha)..."
  STAGE="$UPDATE_ROOT/initial-install"
  rm -rf "$STAGE"
  mkdir -p "$STAGE"
  ARCHIVE="$UPDATE_ROOT/.ai-ch-source.tar.gz"
  download_private_archive "https://api.github.com/repos/$REPO/tarball/$commit_sha" "$ARCHIVE"
  tar -xzf "$ARCHIVE" -C "$STAGE"
  rm -f "$ARCHIVE"
  SOURCE_DIR=$(find "$STAGE" -mindepth 1 -maxdepth 1 -type d | head -n 1)
  if [ -z "$SOURCE_DIR" ] || [ ! -f "$SOURCE_DIR/app/main.py" ] || \
     [ ! -f "$SOURCE_DIR/frontend/index.html" ] || [ ! -f "$SOURCE_DIR/requirements.txt" ]; then
    echo "Архив GitHub не содержит app/main.py, frontend/index.html и requirements.txt." >&2
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

  echo "Скачиваю базовый образ python:3.12-slim (сборка образа не нужна)..."
  docker pull python:3.12-slim
  echo "Создаю контейнер $container_name..."
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
  rm -rf "$STAGE" "$META_FILE"
  echo "Запускаю ai-ch; первая установка Python-зависимостей может занять несколько минут..."
  docker start "$container_name" >/dev/null
  healthy=false
  attempts=0
  while [ "$attempts" -lt 60 ]; do
    if curl --fail --silent --show-error --max-time 4 "$HEALTH_URL" >/dev/null 2>&1; then
      healthy=true
      break
    fi
    attempts=$((attempts + 1))
    sleep 3
  done
  if [ "$healthy" != "true" ]; then
    echo "ai-ch не ответил на $HEALTH_URL за 3 минуты." >&2
    echo "Контейнер оставлен для диагностики; данные и .env не удалены." >&2
    echo "Проверь: docker logs --tail 150 $container_name" >&2
    exit 1
  fi

  # Mark the exact installed commit as applied so the timer does not immediately
  # copy the same source into the just-started container and restart it.
  python3 - "$UPDATE_ROOT/state.json" "$commit_sha" <<'PY'
import json,sys
from datetime import datetime,timezone
path,sha=sys.argv[1:]
now=datetime.now(timezone.utc).isoformat(timespec="seconds")
with open(path,"w",encoding="utf-8") as f:
    json.dump({"remote_sha":sha,"applied_sha":sha,"last_check":now,"last_success":now,
               "last_error":None,"failed_sha":None,"dependency_sha":None,
               "dependency_history":[]},f,ensure_ascii=False,indent=2)
    f.write("\n")
PY
else
  echo "Контейнер $container_name уже существует — не пересоздаю и не удаляю его."
  running=$(docker inspect --format='{{.State.Running}}' "$container_name" 2>/dev/null || true)
  if [ "$running" != "true" ]; then
    echo "Существующий контейнер не запущен. Запусти его вручную и повтори установку." >&2
    exit 1
  fi
  if ! docker exec "$container_name" test -f /app/app/main.py || \
     ! docker exec "$container_name" test -f /app/frontend/index.html; then
    echo "Контейнер существует, но в нём не найдена структура ai-ch; ничего не перезаписывал." >&2
    exit 1
  fi
fi

# Ensure the container survives host shutdowns/reboots, including containers
# created by an older version of this installer.
docker update --restart unless-stopped "$container_name" >/dev/null

echo "Устанавливаю службу восстановления после загрузки ZimaOS..."
cat > "$UPDATE_ROOT/recover_container.sh" <<'RECOVERY'
#!/bin/sh
set -eu
CONTAINER="${DOCKER_CONTAINER:-ai-ch}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI не найден; AI CH не запускался." >&2
  exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Контейнер $CONTAINER не найден; восстановление пропущено." >&2
  exit 0
fi

running=$(docker inspect --format='{{.State.Running}}' "$CONTAINER")
if [ "$running" != "true" ]; then
  echo "Контейнер $CONTAINER остановлен; запускаю его после старта Docker."
  docker start "$CONTAINER" >/dev/null
else
  echo "Контейнер $CONTAINER уже запущен."
fi
RECOVERY
chmod 755 "$UPDATE_ROOT/recover_container.sh"

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

echo "Устанавливаю службу автоматических обновлений..."
curl --fail --silent --show-error --location "$RAW_BASE/auto_deploy_daemon.py" \
  -o "$UPDATE_ROOT/auto_deploy_daemon.py"
chmod 755 "$UPDATE_ROOT/auto_deploy_daemon.py"

cat > /etc/ai-ch-updater.env <<EOF
GITHUB_REPO=$REPO
GITHUB_BRANCH=$BRANCH
GITHUB_TOKEN=$GITHUB_TOKEN
TARGET_MODE=docker
DOCKER_CONTAINER=$container_name
APP_DIR=/app
HEALTH_URL=$HEALTH_URL
HEALTH_TIMEOUT=120
HTTP_TIMEOUT=25
KEEP_RELEASES=3
UPDATE_ROOT=$UPDATE_ROOT
EOF
chmod 600 /etc/ai-ch-updater.env
rm -f "$META_FILE"

cat > /etc/systemd/system/ai-ch-updater.service <<EOF
[Unit]
Description=ai-ch GitHub updater (staged update with health-check rollback)
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
systemctl enable ai-ch-recovery.service
systemctl start ai-ch-recovery.service
systemctl enable --now ai-ch-updater.timer

echo
echo "Готово: AI CH установлен и отвечает на $HEALTH_URL"
echo "Контейнер: $container_name"
echo "Постоянные данные: $APP_ROOT/data и $APP_ROOT/uploads"
echo "Секреты приложения: $APP_ROOT/.env (права 600; не публикуй этот файл)"
echo "Токен GitHub: /etc/ai-ch-updater.env (права 600; не копируется в контейнер)"
echo "Автообновление приватного репозитория: каждые 3 минуты, ветка $REPO/$BRANCH"
echo "Статус: systemctl status ai-ch-updater.timer"
echo "Логи приложения: docker logs --tail 100 $container_name"
echo "Логи обновлятора: $UPDATE_ROOT/logs/updater.log"
echo "Адрес: http://<IP-ZimaOS>:$health_port/"
echo "Примечание: отдельная песочница кода не устанавливается этим сценарием."
