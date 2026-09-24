#!/usr/bin/env python3
"""Start OmniRoute in Docker and point it at local Ollama. No dashboard steps."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

from reddit_joiner.paths import ENV_FILE, OMNIROUTE_DIR

COMPOSE_DIR = OMNIROUTE_DIR
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
GATEWAY_ENV = COMPOSE_DIR / ".env"
REDDIT_ENV = ENV_FILE
HOST = os.environ.get("OMNIROUTE_HOST", "http://127.0.0.1:20128").rstrip("/")
DEFAULT_PASSWORD = "CHANGEME"
LogFn = Callable[[str], None]


def _log(log: Optional[LogFn], message: str) -> None:
    if log:
        log(message)


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _upsert_dotenv(path: Path, updates: dict[str, str]) -> None:
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _gateway_env() -> dict[str, str]:
    values = _read_dotenv(GATEWAY_ENV)
    changed = False
    if not values.get("JWT_SECRET"):
        values["JWT_SECRET"] = secrets.token_urlsafe(48)
        changed = True
    if not values.get("API_KEY_SECRET"):
        values["API_KEY_SECRET"] = secrets.token_hex(32)
        changed = True
    if not values.get("INITIAL_PASSWORD"):
        values["INITIAL_PASSWORD"] = DEFAULT_PASSWORD
        changed = True
    values["REQUIRE_API_KEY"] = "false"
    if changed or not GATEWAY_ENV.is_file():
        GATEWAY_ENV.parent.mkdir(parents=True, exist_ok=True)
        GATEWAY_ENV.write_text(
            "\n".join(f"{key}={values[key]}" for key in (
                "JWT_SECRET",
                "API_KEY_SECRET",
                "INITIAL_PASSWORD",
                "REQUIRE_API_KEY",
            ))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(GATEWAY_ENV, 0o600)
    return values


def _compose(*args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=str(COMPOSE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _container_has_jwt() -> bool:
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "omniroute",
                "sh",
                "-c",
                'test -n "$JWT_SECRET"',
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return result.returncode == 0
    except Exception:
        return False


def _start_container(recreate: bool = False) -> None:
    args = ["up", "-d"]
    if recreate:
        args.append("--force-recreate")
    result = _compose(*args, timeout=120)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "docker compose failed").strip().splitlines()
        raise RuntimeError(err[-1] if err else "docker compose failed")


def _wait_healthy(seconds: float = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            response = requests.get(f"{HOST}/healthz", timeout=3)
            if response.ok:
                return True
        except Exception:
            pass
        time.sleep(1.2)
    return False


def _current_key() -> str:
    env = _read_dotenv(REDDIT_ENV)
    return (
        os.environ.get("OMNIROUTE_API_KEY", "").strip()
        or env.get("OMNIROUTE_API_KEY", "").strip()
        or env.get("OMNIROUTE_KEY", "").strip()
        or "sk_omniroute"
    )


def _models_ok(api_key: str) -> bool:
    try:
        response = requests.get(
            f"{HOST}/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        if response.status_code >= 400:
            return False
        rows = (response.json() or {}).get("data") or []
        return any(
            ("ollama/" in str((row or {}).get("id") or "")
             or "ollama-local/" in str((row or {}).get("id") or ""))
            and "mistral" in str((row or {}).get("id") or "")
            for row in rows
        )
    except Exception:
        return False


def _apply_key(api_key: str) -> None:
    os.environ["OMNIROUTE_API_KEY"] = api_key
    try:
        from reddit_joiner import ai as reddit_ai

        reddit_ai.OMNIROUTE_API_KEY = api_key
    except Exception:
        pass
    _upsert_dotenv(REDDIT_ENV, {"OMNIROUTE_API_KEY": api_key, "OMNIROUTE_HOST": HOST})


def _dashboard_password(gateway: dict[str, str]) -> str:
    return (
        os.environ.get("OMNIROUTE_DASHBOARD_PASSWORD", "").strip()
        or gateway.get("INITIAL_PASSWORD", "").strip()
        or DEFAULT_PASSWORD
    )


def _login(session: requests.Session, password: str) -> bool:
    try:
        response = session.post(
            f"{HOST}/api/auth/login",
            json={"password": password},
            timeout=12,
        )
    except Exception:
        return False
    return response.ok


def _json(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _add_ollama(session: requests.Session) -> None:
    listed = session.get(f"{HOST}/api/providers?provider=ollama-local&limit=50", timeout=12)
    body = _json(listed)
    rows = body.get("connections") or body.get("providers") or []
    connection_id = ""
    for row in rows:
        if str((row or {}).get("provider") or "") == "ollama-local":
            connection_id = str((row or {}).get("id") or "")
            break
    if not connection_id:
        created = session.post(
            f"{HOST}/api/providers",
            json={
                "provider": "ollama-local",
                "name": "Ollama",
                "defaultModel": os.environ.get("OLLAMA_MODEL", "mistral:7b"),
                "providerSpecificData": {"baseUrl": "http://127.0.0.1:11434/v1"},
            },
            timeout=20,
        )
        payload = _json(created)
        connection = payload.get("connection") if isinstance(payload.get("connection"), dict) else payload
        connection_id = str((connection or {}).get("id") or "")
        if created.status_code >= 400 or not connection_id:
            raise RuntimeError("could not add local Ollama to OmniRoute")
    session.post(
        f"{HOST}/api/providers/{connection_id}/test",
        json={"validationModelId": os.environ.get("OLLAMA_MODEL", "mistral:7b")},
        timeout=45,
    )


def _bootstrap(log: Optional[LogFn]) -> str:
    gateway = _gateway_env()
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    if not _login(session, _dashboard_password(gateway)):
        raise RuntimeError("OmniRoute dashboard login failed")
    session.post(
        f"{HOST}/api/settings/require-login",
        json={"requireLogin": False},
        timeout=12,
    )
    created = session.post(
        f"{HOST}/api/keys",
        json={"name": "reddit-joiner", "scopes": ["manage"]},
        timeout=15,
    )
    payload = _json(created)
    api_key = str(payload.get("key") or "").strip()
    if created.status_code >= 400 or not api_key:
        raise RuntimeError("could not create an OmniRoute API key")
    _apply_key(api_key)
    _add_ollama(session)
    deadline = time.time() + 25
    while time.time() < deadline:
        if _models_ok(api_key):
            return api_key
        time.sleep(1.2)
    if _models_ok(api_key):
        return api_key
    _log(log, "OmniRoute key saved; Ollama may still be connecting")
    return api_key


def ensure_omniroute(log: Optional[LogFn] = None) -> bool:
    """Start the official OmniRoute image and connect local Ollama. True if /v1 works."""
    if not COMPOSE_FILE.is_file():
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=8, check=True)
    except Exception:
        _log(log, "Docker is not running — comments will use Ollama directly")
        return False

    host = urlparse(HOST).hostname or ""
    if host not in {"127.0.0.1", "localhost"}:
        return _models_ok(_current_key())

    try:
        _gateway_env()
        need_recreate = not _container_has_jwt()
        _log(log, "Starting OmniRoute (Docker)…")
        _start_container(recreate=need_recreate)
        if not _wait_healthy():
            _log(log, "OmniRoute did not become healthy — using Ollama")
            return False
        if _models_ok(_current_key()):
            return True
        _bootstrap(log)
        return _models_ok(_current_key())
    except Exception as exc:
        _log(log, f"OmniRoute setup skipped ({exc}) — using Ollama")
        return False
