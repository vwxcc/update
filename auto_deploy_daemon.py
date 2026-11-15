#!/usr/bin/env python3
"""Safe, one-shot updater for ai-ch running in an existing Docker container.

The updater never runs git pull, never recreates the target container, and never
writes to the app's configured data/upload directories. It stages a GitHub commit,
validates Python syntax, snapshots the live app, then swaps code while stopped.
A failed health check restores the previous app files and dependency selection.
"""
from __future__ import annotations

import ast
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

GITHUB_REPO = os.getenv("GITHUB_REPO", "vwxcc/deepseekw").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
TARGET_MODE = os.getenv("TARGET_MODE", "docker").strip().lower()
DOCKER_CONTAINER = os.getenv("DOCKER_CONTAINER", "ai-ch-mega").strip()
APP_DIR = os.getenv("APP_DIR", "/app").rstrip("/")
HEALTH_URL = os.getenv("HEALTH_URL", "http://127.0.0.1:3050/api/health").strip()
CONTAINER_PORT = max(1, min(65535, int(os.getenv("CONTAINER_PORT", "8000"))))
UPDATER_VERSION = 3
HEALTH_TIMEOUT = max(15, int(os.getenv("HEALTH_TIMEOUT", "90")))
HTTP_TIMEOUT = max(5, int(os.getenv("HTTP_TIMEOUT", "25")))
KEEP_RELEASES = max(2, min(10, int(os.getenv("KEEP_RELEASES", "3"))))
UPDATE_ROOT = Path(os.getenv("UPDATE_ROOT", "/DATA/AppData/ai-ch-updater")).expanduser()
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
ARCHIVE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/tarball/"
USER_AGENT = "ai-ch-zimaos-updater/1.0"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIREMENTS_FILENAME = "requirements.txt"
PTH_FILENAME = "00-ai-ch-updater.pth"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def logger_setup() -> logging.Logger:
    log_dir = UPDATE_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ai_ch_updater")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        file_handler = logging.FileHandler(log_dir / "updater.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(console)
        logger.addHandler(file_handler)
    return logger


LOG: logging.Logger


def run_cmd(args: list[str], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        details = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        # Commands are passed as argument arrays; no shell evaluation is performed.
        raise RuntimeError(f"Команда {args[0]} завершилась с ошибкой: {details[-1800:]}")
    return result


def docker_exec(*args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_cmd(["docker", "exec", DOCKER_CONTAINER, *args], timeout=timeout, check=check)


def docker_running() -> bool:
    result = run_cmd(
        ["docker", "inspect", "--format={{.State.Running}}", DOCKER_CONTAINER],
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Контейнер «{DOCKER_CONTAINER}» не найден: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip().lower() == "true"


def read_state() -> dict[str, Any]:
    path = UPDATE_ROOT / "state.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(f"Не удалось прочитать состояние обновлятора: {path}")


def write_state(state: dict[str, Any]) -> None:
    path = UPDATE_ROOT / "state.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def github_headers(*, accept: str = "application/vnd.github+json") -> dict[str, str]:
    """Build private-repository API headers without ever logging the token."""
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "Не задан GITHUB_TOKEN. Создайте fine-grained GitHub token с Contents: Read-only "
            "только для vwxcc/deepseekw и сохраните его в /etc/ai-ch-updater.env (права 600)."
        )
    if not re.fullmatch(r"[A-Za-z0-9_]+", GITHUB_TOKEN):
        raise RuntimeError("GITHUB_TOKEN содержит недопустимые символы; секрет не был отправлен.")
    return {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class SafeGitHubArchiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only HTTPS GitHub archive redirects and strip auth across hosts."""

    ALLOWED_HOSTS = {"api.github.com", "codeload.github.com"}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urlsplit(req.full_url)
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in self.ALLOWED_HOSTS:
            raise urllib.error.URLError("GitHub returned an untrusted archive redirect.")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and source.hostname != target.hostname:
            for name in list(redirected.headers):
                if name.lower() in {"authorization", "proxy-authorization", "cookie"}:
                    redirected.remove_header(name)
        return redirected


def github_http_error(exc: urllib.error.HTTPError, *, downloading: bool = False) -> RuntimeError:
    if exc.code == 401:
        message = "GitHub отклонил GITHUB_TOKEN: токен неверен, истёк или отозван."
    elif exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
        message = "GitHub API исчерпал лимит запросов; следующая проверка будет выполнена таймером."
    elif exc.code == 403:
        message = "GitHub запретил доступ. Проверьте доступ токена к vwxcc/deepseekw и разрешение Contents: Read-only."
    elif exc.code == 404:
        message = "GitHub вернул 404. Для приватного репозитория это обычно означает, что токен не имеет доступа к vwxcc/deepseekw."
    else:
        action = "скачать архив" if downloading else "проверить ветку"
        message = f"GitHub API вернул HTTP {exc.code}, не удалось {action}."
    return RuntimeError(message)


def github_latest(state: dict[str, Any]) -> tuple[str, str | None]:
    headers = github_headers()
    if state.get("etag"):
        headers["If-None-Match"] = str(state["etag"])
    request = urllib.request.Request(API_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
            sha = str(payload.get("sha", "")).lower()
            if not SHA_RE.fullmatch(sha):
                raise RuntimeError("GitHub вернул некорректный SHA коммита.")
            return sha, response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and SHA_RE.fullmatch(str(state.get("remote_sha", ""))):
            return str(state["remote_sha"]), str(state.get("etag", "")) or None
        raise github_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось проверить GitHub: {exc}") from exc


def download_archive(sha: str, destination: Path) -> None:
    # Use the authenticated GitHub API archive endpoint. GitHub redirects to a
    # short-lived codeload URL; urllib strips Authorization on cross-host redirects.
    request = urllib.request.Request(
        ARCHIVE_API_URL + sha,
        headers=github_headers(accept="application/vnd.github+json"),
    )
    try:
        with urllib.request.build_opener(SafeGitHubArchiveRedirectHandler()).open(
            request, timeout=max(HTTP_TIMEOUT, 120)
        ) as response:
            if urlsplit(response.geturl()).hostname not in SafeGitHubArchiveRedirectHandler.ALLOWED_HOSTS:
                raise RuntimeError("GitHub redirected the archive to an untrusted host.")
            content = response.read(100 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise github_http_error(exc, downloading=True) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Не удалось скачать исходный код коммита {sha[:12]}: {exc}") from exc
    if not content or len(content) > 100 * 1024 * 1024:
        raise RuntimeError("Архив GitHub пустой либо превышает допустимый размер 100 MiB.")
    destination.write_bytes(content)


def extract_release(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    allowed_top = {"app", "frontend", REQUIREMENTS_FILENAME}
    allowed_files = 0
    with tarfile.open(archive, mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 50000:
            raise RuntimeError("В архиве слишком много файлов; обновление остановлено.")
        for member in members:
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2 or parts[0] in ("", ".", ".."):
                continue
            relative = PurePosixPath(*parts[1:])
            if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
                raise RuntimeError("В архиве обнаружен небезопасный путь.")
            if relative.parts[0] not in allowed_top:
                continue
            if not member.isdir() and not member.isfile():
                raise RuntimeError(f"Обновление содержит неподдерживаемый тип файла: {member.name}")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Путь архива выходит за пределы каталога релиза.")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"Не удалось прочитать файл из архива: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            allowed_files += 1
    if allowed_files == 0:
        raise RuntimeError("В архиве не найдены файлы приложения.")
    validate_release(destination)


def validate_release(path: Path) -> None:
    required = [
        path / "app" / "main.py",
        path / "frontend" / "index.html",
        path / REQUIREMENTS_FILENAME,
    ]
    missing = [str(item.relative_to(path)) for item in required if not item.is_file()]
    if missing:
        raise RuntimeError("Релиз неполный; отсутствуют: " + ", ".join(missing))
    python_files = list((path / "app").rglob("*.py"))
    if not python_files:
        raise RuntimeError("В релизе нет Python-файлов приложения.")
    for file_path in python_files:
        try:
            ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            raise RuntimeError(f"Проверка Python не пройдена ({file_path.relative_to(path)}): {exc}") from exc
    req_text = (path / REQUIREMENTS_FILENAME).read_text(encoding="utf-8")
    if not req_text.strip():
        raise RuntimeError("requirements.txt пуст; обновление остановлено.")
    if (path / "app" / "main.py").stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("app/main.py имеет подозрительно большой размер.")


def copy_from_container(source: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    run_cmd(["docker", "cp", f"{DOCKER_CONTAINER}:{source.rstrip('/')}/.", str(destination)], timeout=60)


def snapshot_container(backup: Path) -> None:
    app_path = f"{APP_DIR}/app"
    frontend_path = f"{APP_DIR}/frontend"
    copy_from_container(app_path, backup / "app")
    copy_from_container(frontend_path, backup / "frontend")
    run_cmd(
        ["docker", "cp", f"{DOCKER_CONTAINER}:{APP_DIR}/{REQUIREMENTS_FILENAME}", str(backup / REQUIREMENTS_FILENAME)],
        timeout=30,
    )
    if not (backup / "app" / "main.py").is_file() or not (backup / "frontend" / "index.html").is_file():
        raise RuntimeError("Резервная копия контейнера неполная; обновление не будет применено.")


def stage_inside_container(release: Path, remote_stage: str) -> None:
    # Remove an incomplete stage left by an earlier failed attempt for this SHA.
    docker_exec("rm", "-rf", remote_stage, timeout=20)
    docker_exec("mkdir", "-p", remote_stage, timeout=15)
    run_cmd(["docker", "cp", str(release / "app"), f"{DOCKER_CONTAINER}:{remote_stage}/"], timeout=60)
    run_cmd(["docker", "cp", str(release / "frontend"), f"{DOCKER_CONTAINER}:{remote_stage}/"], timeout=60)
    run_cmd(
        ["docker", "cp", str(release / REQUIREMENTS_FILENAME), f"{DOCKER_CONTAINER}:{remote_stage}/{REQUIREMENTS_FILENAME}"],
        timeout=30,
    )
    docker_exec("python", "-m", "compileall", "-q", f"{remote_stage}/app", timeout=60)


def current_site_packages() -> str:
    result = docker_exec(
        "python", "-c", "import site; print(site.getsitepackages()[0])", timeout=15
    )
    value = result.stdout.strip().splitlines()[-1]
    if not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise RuntimeError("Не удалось безопасно определить site-packages внутри контейнера.")
    return value.rstrip("/")


def dependency_pointer(dependency_sha: str | None) -> str:
    if dependency_sha and SHA_RE.fullmatch(dependency_sha):
        return f"import sys; sys.path.insert(0, '/opt/ai-ch-deps/{dependency_sha}')\n"
    return ""


def prepare_dependency_set(remote_stage: str, sha: str, release: Path, requirements_changed: bool) -> str | None:
    if not requirements_changed:
        return None
    target = f"/opt/ai-ch-deps/{sha}"
    # A fresh, versioned directory isolates dependency changes from the globally
    # installed packages. Rollback only has to restore the old .pth pointer.
    docker_exec("rm", "-rf", target, timeout=20)
    docker_exec("mkdir", "-p", target, timeout=15)
    LOG.info("Изменился requirements.txt — устанавливаю зависимости в отдельный каталог версии.")
    docker_exec(
        "python", "-m", "pip", "install", "--no-cache-dir", "--target", target,
        "-r", f"{remote_stage}/{REQUIREMENTS_FILENAME}", timeout=900,
    )
    if not docker_exec("test", "-d", target, timeout=10).returncode == 0:
        raise RuntimeError("Каталог изолированных зависимостей не создан.")
    return sha



def smoke_test_release(remote_stage: str, dependency_sha: str | None) -> None:
    """Import the staged app and execute FastAPI lifespan without touching live data."""
    dependency_path = f"/opt/ai-ch-deps/{dependency_sha}" if dependency_sha else ""
    script = f"""
import asyncio
import sys
sys.path.insert(0, {remote_stage!r})
if {dependency_path!r}:
    sys.path.insert(0, {dependency_path!r})
from app.main import app

async def check_startup():
    async with app.router.lifespan_context(app):
        pass

asyncio.run(check_startup())
print("AI CH staged import/startup check: OK")
"""
    LOG.info("Проверяю импорт новой версии и startup FastAPI в изолированной рабочей папке.")
    result = run_cmd(
        [
            "docker", "exec",
            "-w", remote_stage,
            DOCKER_CONTAINER,
            "env", "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED=1",
            "PYTHONDONTWRITEBYTECODE=1",
            "python", "-c", script,
        ],
        timeout=120,
        check=False,
    )
    if result.returncode:
        details = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        LOG.error("Новая версия не прошла предварительный импорт/startup check: %s", details[-5000:])
        raise RuntimeError(
            "Новая версия не прошла предварительную проверку импорта/startup. "
            "Работающий контейнер не останавливался; подробности в логе updater."
        )
    LOG.info("Предварительная проверка импорта и startup пройдена: %s.", result.stdout.strip()[-500:])

def replace_app_files(source_root: Path) -> None:
    # docker cp can write to a stopped container. Copy directory contents into
    # the already-existing destinations; this also lets rollback work when the
    # application process failed to start and docker exec is unavailable.
    # Unreferenced files removed upstream can remain as harmless orphans; the
    # current app/frontend entrypoints determine which files are actually used.
    run_cmd(["docker", "cp", str(source_root / "app") + "/.", f"{DOCKER_CONTAINER}:{APP_DIR}/app/"], timeout=90)
    run_cmd(["docker", "cp", str(source_root / "frontend") + "/.", f"{DOCKER_CONTAINER}:{APP_DIR}/frontend/"], timeout=90)
    run_cmd(
        ["docker", "cp", str(source_root / REQUIREMENTS_FILENAME), f"{DOCKER_CONTAINER}:{APP_DIR}/{REQUIREMENTS_FILENAME}"],
        timeout=30,
    )


def write_dependency_pointer(dependency_sha: str | None, site_packages: str, work_dir: Path) -> None:
    pth = work_dir / PTH_FILENAME
    pth.write_text(dependency_pointer(dependency_sha), encoding="utf-8")
    run_cmd(
        ["docker", "cp", str(pth), f"{DOCKER_CONTAINER}:{site_packages}/{PTH_FILENAME}"],
        timeout=30,
    )


def wait_for_health() -> bool:
    """Check the app from inside the selected container; host port mappings are irrelevant."""
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_error = "ответ не получен"
    health_script = (
        "import urllib.request; "
        f"r=urllib.request.urlopen('http://127.0.0.1:{CONTAINER_PORT}/api/health', timeout=4); "
        "raise SystemExit(0 if 200 <= r.status < 300 else 1)"
    )
    LOG.info(
        "Проверяю /api/health внутри контейнера %s на порту %s.",
        DOCKER_CONTAINER,
        CONTAINER_PORT,
    )
    while time.monotonic() < deadline:
        result = docker_exec("python", "-c", health_script, timeout=8, check=False)
        if result.returncode == 0:
            return True
        last_error = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()[-500:]
        time.sleep(3)
    LOG.error(
        "Внутренняя проверка здоровья не пройдена: контейнер=%s, порт=%s, причина=%s",
        DOCKER_CONTAINER,
        CONTAINER_PORT,
        last_error,
    )
    return False



def container_stop_if_running() -> None:
    if docker_running():
        run_cmd(["docker", "stop", "--time", "20", DOCKER_CONTAINER], timeout=40)


def container_start() -> None:
    run_cmd(["docker", "start", DOCKER_CONTAINER], timeout=40)


def rollback(
    backup: Path,
    state: dict[str, Any],
    site_packages: str,
    work_dir: Path,
    failed_release: Path | None = None,
) -> bool:
    LOG.error("Откат: возвращаю предыдущие app/frontend/requirements.txt и выбор зависимостей.")
    try:
        container_stop_if_running()
    except Exception as exc:
        LOG.warning("Остановка перед откатом сообщила: %s", exc)
    try:
        replace_app_files(backup)
        write_dependency_pointer(state.get("dependency_sha"), site_packages, work_dir)
        container_start()
        if failed_release is not None:
            # Drop files introduced by the rejected release before accepting rollback.
            prune_removed_files(failed_release, backup)
        if wait_for_health():
            LOG.warning("Откат завершён; предыдущая версия отвечает на health-check.")
            return True
        LOG.critical("После отката health-check также не прошёл. Проверьте журнал контейнера вручную.")
        return False
    except Exception:
        LOG.exception("Откат завершился ошибкой; требуется ручная проверка контейнера.")
        return False


def prune_releases(applied_sha: str, remote_sha: str) -> None:
    releases = UPDATE_ROOT / "releases"
    if not releases.exists():
        return
    dirs = sorted(
        (item for item in releases.iterdir() if item.is_dir() and SHA_RE.fullmatch(item.name)),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    keep = {applied_sha, remote_sha}
    keep.update(item.name for item in dirs[:KEEP_RELEASES])
    for item in dirs:
        if item.name not in keep:
            shutil.rmtree(item, ignore_errors=True)



def prune_removed_files(previous_root: Path, release_root: Path) -> None:
    """Remove source files deleted upstream after the new version passed health-check."""
    stale: list[str] = []
    for area in ("app", "frontend"):
        previous = previous_root / area
        current = release_root / area
        if not previous.is_dir() or not current.is_dir():
            continue
        current_files = {
            item.relative_to(current).as_posix()
            for item in current.rglob("*")
            if item.is_file()
        }
        for item in previous.rglob("*"):
            if not item.is_file():
                continue
            relative = item.relative_to(previous).as_posix()
            if relative not in current_files and ".." not in PurePosixPath(relative).parts:
                stale.append(f"{APP_DIR}/{area}/{relative}")

    if not stale:
        return
    cleanup_script = (
        "import os,sys; "
        "[(os.remove(p) if os.path.isfile(p) or os.path.islink(p) else None) "
        "for p in sys.argv[1:]]"
    )
    try:
        result = docker_exec("python", "-c", cleanup_script, *stale, timeout=90, check=False)
        if result.returncode:
            LOG.warning(
                "Не удалось полностью удалить %s устаревших файлов исходного кода: %s",
                len(stale),
                (result.stderr or result.stdout).strip()[-500:],
            )
        else:
            LOG.info("Удалено устаревших файлов из app/frontend: %s.", len(stale))
    except Exception as exc:
        LOG.warning("Очистка устаревших исходных файлов пропущена: %s", exc)

def perform_update(sha: str, state: dict[str, Any]) -> dict[str, Any]:
    if TARGET_MODE != "docker":
        raise RuntimeError("Поддерживается TARGET_MODE=docker. Другой режим не реализован в этой версии.")
    if not DOCKER_CONTAINER:
        raise RuntimeError("Не задано имя контейнера DOCKER_CONTAINER.")
    if not APP_DIR.startswith("/") or ".." in PurePosixPath(APP_DIR).parts:
        raise RuntimeError("APP_DIR должен быть безопасным абсолютным путём.")
    if not docker_running():
        raise RuntimeError(f"Контейнер «{DOCKER_CONTAINER}» не запущен; обновление отложено.")
    releases = UPDATE_ROOT / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / sha
    if not release.exists():
        archive_file = UPDATE_ROOT / f".source-{sha}.tar.gz"
        try:
            LOG.info("Скачиваю исходный код GitHub %s, commit %s.", GITHUB_REPO, sha[:12])
            download_archive(sha, archive_file)
            extract_release(archive_file, release)
        finally:
            archive_file.unlink(missing_ok=True)
    else:
        validate_release(release)

    work_dir = Path(tempfile.mkdtemp(prefix="ai-ch-update-", dir=str(UPDATE_ROOT)))
    backup = UPDATE_ROOT / "backups" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sha[:12]}"
    remote_stage = f"/tmp/ai-ch-update-{sha}"
    site_packages = current_site_packages()
    dependency_sha = state.get("dependency_sha")
    dependency_changed = False
    app_was_stopped = False
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            shutil.rmtree(backup)
        snapshot_container(backup)
        old_requirements_sha = sha256_file(backup / REQUIREMENTS_FILENAME)
        new_requirements_sha = sha256_file(release / REQUIREMENTS_FILENAME)
        requirements_changed = old_requirements_sha != new_requirements_sha
        LOG.info("Подготавливаю файлы во временном каталоге контейнера; работающий сервер пока не останавливается.")
        stage_inside_container(release, remote_stage)
        if requirements_changed:
            dependency_sha = prepare_dependency_set(remote_stage, sha, release, True)
            dependency_changed = True
        else:
            LOG.info("requirements.txt не изменился — зависимости не устанавливаются.")

        # Import and run the app startup in a temporary cwd with a cleared environment.
        # Its default SQLite database is created under the staging directory, never /app/data.
        smoke_test_release(remote_stage, dependency_sha if dependency_changed else None)

        # The existing process keeps serving until all validation and downloads are done.
        container_stop_if_running()
        app_was_stopped = True
        replace_app_files(release)
        if dependency_changed:
            write_dependency_pointer(dependency_sha, site_packages, work_dir)
        container_start()
        app_was_stopped = False

        if not wait_for_health():
            LOG.error("Новая версия не прошла внутренний health-check; запускаю автоматический откат.")
            # If restoring old files fails because the new code is crashing, docker
            # cp still works against a stopped container; the pointer is restored
            # before starting the old code again.
            restored = rollback(backup, state, site_packages, work_dir, release)
            state["failed_sha"] = sha
            state["failed_updater_version"] = UPDATER_VERSION
            state["last_error"] = "внутренняя health-проверка новой версии не пройдена"
            state["last_check"] = utc_now()
            if not restored:
                state["rollback_failed"] = True
            write_state(state)
            raise RuntimeError("Обновление отменено; предыдущая версия восстановлена." if restored else
                               "Обновление не прошло проверку, и автоматический откат не подтвердился.")
        prune_removed_files(backup, release)
        state["applied_sha"] = sha
        state["dependency_sha"] = dependency_sha
        state["requirements_sha"] = new_requirements_sha
        state["last_success"] = utc_now()
        state["last_error"] = None
        state["failed_sha"] = None
        state["failed_updater_version"] = None
        state["rollback_failed"] = False
        history = list(state.get("dependency_history", []))
        if dependency_changed and dependency_sha:
            history = [dependency_sha] + [item for item in history if item != dependency_sha]
        state["dependency_history"] = history[:KEEP_RELEASES]
        write_state(state)
        LOG.info("Обновление успешно применено: %s.", sha)
        cleanup_remote_stage(remote_stage)
        cleanup_dependency_dirs(state.get("dependency_history", []), state.get("dependency_sha"))
        prune_releases(sha, sha)
        prune_backups(backup)
        return state
    except Exception:
        # If failure happened after stopping but before a successful start, try to
        # restore the old version here unless rollback was already performed.
        if app_was_stopped:
            LOG.exception("Ошибка во время переключения версии.")
            try:
                rollback(backup, state, site_packages, work_dir, release)
            except Exception:
                LOG.exception("Не удалось выполнить резервный откат.")
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def cleanup_remote_stage(remote_stage: str) -> None:
    try:
        if docker_running():
            docker_exec("rm", "-rf", remote_stage, timeout=20, check=False)
    except Exception:
        pass


def cleanup_dependency_dirs(history: list[str], current_sha: str | None) -> None:
    # Dependency directories are versioned, and old directories are deleted only
    # after the new version has passed its health check.
    keep = {item for item in history if SHA_RE.fullmatch(str(item))}
    if current_sha and SHA_RE.fullmatch(str(current_sha)):
        keep.add(str(current_sha))
    try:
        if not docker_running():
            return
        result = docker_exec("sh", "-c", "find /opt/ai-ch-deps -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null || true",
                             timeout=15, check=False)
        for name in result.stdout.splitlines():
            if SHA_RE.fullmatch(name) and name not in keep:
                docker_exec("rm", "-rf", f"/opt/ai-ch-deps/{name}", timeout=30, check=False)
    except Exception:
        LOG.warning("Не удалось удалить старые каталоги зависимостей; приложение продолжает работу.")


def prune_backups(current: Path) -> None:
    base = UPDATE_ROOT / "backups"
    if not base.exists():
        return
    dirs = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    keep = {current}
    keep.update(dirs[:KEEP_RELEASES])
    for item in dirs:
        if item not in keep:
            shutil.rmtree(item, ignore_errors=True)


def failed_commit_for_current_updater(state: dict[str, Any], sha: str) -> bool:
    """Only suppress retries after this updater version has already rejected the SHA."""
    return (
        state.get("failed_sha") == sha
        and state.get("failed_updater_version") == UPDATER_VERSION
    )


def main() -> int:
    global LOG
    UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
    LOG = logger_setup()
    lock_path = UPDATE_ROOT / "updater.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Другая проверка ещё выполняется; этот запуск пропускаю.")
            return 0
        state: dict[str, Any] = {}
        try:
            state = read_state()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", GITHUB_REPO):
                raise RuntimeError("GITHUB_REPO должен иметь вид owner/repository.")
            if not GITHUB_BRANCH:
                raise RuntimeError("GITHUB_BRANCH не задан.")
            latest, etag = github_latest(state)
            state["remote_sha"] = latest
            state["etag"] = etag
            state["last_check"] = utc_now()
            if state.get("applied_sha") == latest:
                state["last_error"] = None
                write_state(state)
                LOG.info("Изменений нет; приложение уже на версии %s.", latest[:12])
                prune_releases(latest, latest)
                return 0
            if failed_commit_for_current_updater(state, latest):
                write_state(state)
                LOG.warning(
                    "Коммит %s уже не прошёл проверку на версии updater %s. "
                    "Автоповтор приостановлен; после исправления updater или очистки failed_sha можно повторить.",
                    latest[:12],
                    UPDATER_VERSION,
                )
                return 0
            LOG.info("Обнаружен коммит %s; начинаю подготовку обновления.", latest[:12])
            perform_update(latest, state)
            return 0
        except Exception as exc:
            LOG.exception("Проверка/обновление завершились ошибкой: %s", exc)
            state["last_check"] = utc_now()
            state["last_error"] = str(exc)[:1000]
            try:
                write_state(state)
            except Exception:
                LOG.exception("Не удалось записать state.json.")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
