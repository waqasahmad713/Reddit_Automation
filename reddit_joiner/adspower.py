#!/usr/bin/env python3
"""AdsPower local API: start, stop, and debugger wait."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

import requests

ADSPOWER_API = os.environ.get("ADSPOWER_API", "http://local.adspower.net:50325").rstrip("/")
ADSPOWER_API_KEY = os.environ.get("ADSPOWER_API_KEY", "").strip()
REQUEST_TIMEOUT = 60
API_MIN_INTERVAL = 1.2

_last_api_call_at = 0.0
_API_LOCK = threading.Lock()


def _api_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if ADSPOWER_API_KEY:
        headers["Authorization"] = f"Bearer {ADSPOWER_API_KEY}"
    return headers


def _throttle_api() -> None:
    global _last_api_call_at
    with _API_LOCK:
        elapsed = time.time() - _last_api_call_at
        if elapsed < API_MIN_INTERVAL:
            time.sleep(API_MIN_INTERVAL - elapsed)
        _last_api_call_at = time.time()


def _joiner():
    from reddit_joiner import joiner

    return joiner


def adspower_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    while_stopping: bool = False,
) -> Dict[str, Any]:
    if not while_stopping and _joiner().stop_requested():
        raise RuntimeError("stopped")
    _throttle_api()
    url = f"{ADSPOWER_API}{path}"
    response = requests.get(
        url, params=params, headers=_api_headers(), timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, None) and str(payload.get("code")) != "0":
        raise RuntimeError(
            f"AdsPower API error on {path}: code={payload.get('code')} "
            f"msg={payload.get('msg')}"
        )
    return payload


def adspower_api_ready() -> bool:
    try:
        payload = requests.get(
            f"{ADSPOWER_API}/status", headers=_api_headers(), timeout=8
        ).json()
        return payload.get("code") in (0, None) or str(payload.get("code")) == "0"
    except Exception:
        try:
            requests.get(
                f"{ADSPOWER_API}/api/v1/user/list",
                params={"page": 1, "page_size": 1},
                timeout=8,
            )
            return True
        except Exception:
            return False


def wait_until_profile_open(user_id: str, label: str, timeout: float = 90.0) -> Dict[str, Any]:
    """Wait until this account's Chrome debugger is ready. Do not require the Opened list."""
    j = _joiner()
    deadline = time.time() + timeout
    data: Dict[str, Any] = {}
    last_log = 0.0
    while time.time() < deadline:
        try:
            payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
            info = payload.get("data") or {}
            selenium = ((info.get("ws") or {}).get("selenium") or "").strip()
            status = str(info.get("status") or "").lower()
            if selenium:
                return info
            if status == "active":
                data = info
        except Exception:
            pass
        now = time.time()
        if now - last_log >= 8:
            j.log(
                f"[Profile {label}] Waiting for the account browser… "
                f"{int(max(0, deadline - now))}s left"
            )
            last_log = now
        time.sleep(1.2)
    return data


def _poll_profile_debugger(user_id: str, seconds: float = 16.0) -> Dict[str, Any]:
    deadline = time.time() + seconds
    data: Dict[str, Any] = {}
    while time.time() < deadline:
        try:
            payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
            info = payload.get("data") or {}
            if ((info.get("ws") or {}).get("selenium") or "").strip():
                return info
            data = info or data
        except Exception:
            pass
        time.sleep(1.4)
    return data


def _start_profile_via_api(user_id: str, label: str) -> Dict[str, Any]:
    j = _joiner()
    last_error: Optional[BaseException] = None
    for attempt in range(1, 4):
        if j.stop_requested():
            raise RuntimeError("stopped")
        existing = _poll_profile_debugger(user_id, seconds=2.0)
        if ((existing.get("ws") or {}).get("selenium") or "").strip():
            return existing
        j.log(
            f"[Profile {label}] Opening the Reddit account browser "
            f"(AdsPower Start API, try {attempt}/3)"
        )
        try:
            payload = adspower_get(
                "/api/v1/browser/start",
                params={
                    "user_id": user_id,
                    "open_tabs": 0,
                    "ip_tab": 1,
                    "new_first_tab": 1,
                    "headless": 0,
                    "cdp_mask": 1,
                },
            )
            return payload.get("data") or {}
        except RuntimeError as exc:
            last_error = exc
            message = str(exc).lower()
            j.log(
                f"[Profile {label}] Start API failed ({j.brief_error(exc)}) — "
                "waiting for the browser"
            )
            waited = _poll_profile_debugger(user_id, seconds=12.0)
            if ((waited.get("ws") or {}).get("selenium") or "").strip():
                return waited
            if "already" in message or "is open" in message or "being used" in message:
                payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
                return payload.get("data") or {}
            time.sleep(2.0 * attempt)
    if last_error:
        raise last_error
    return {}


def start_profile(
    user_id: str,
    label: str,
    name: str = "",
    serial: str = "",
) -> Dict[str, Any]:
    """Open the profile browser. Activity never starts in the AdsPower manager."""
    j = _joiner()
    already = j.opened_profile_ids()
    if user_id in already:
        j.log(f"[Profile {label}] Account already open — attaching")
        payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
        data = payload.get("data") or {}
        if ((data.get("ws") or {}).get("selenium") or "").strip():
            return data

    if not j.PARALLEL_PROFILES:
        try:
            j.focus_adspower_window()
        except Exception:
            pass

    clicked = False
    if not j.PARALLEL_PROFILES:
        try:
            clicked = j.click_open_in_adspower(name, serial, user_id, label)
        except Exception as exc:
            j.log(f"[Profile {label}] Open click skipped ({j.brief_error(exc)})")
    else:
        j.log(f"[Profile {label}] Opening via AdsPower Start API (parallel)")

    data: Dict[str, Any] = {}
    if clicked:
        j.wait_for_adspower_proxy_phase(label)
        j.log(f"[Profile {label}] Waiting for the account browser after proxy assign/refresh")
        data = wait_until_profile_open(user_id, label, timeout=90.0)
        if ((data.get("ws") or {}).get("selenium") or "").strip():
            j.log(f"[Profile {label}] Account browser is open")
            return data
        if user_id in j.opened_profile_ids():
            j.log(
                f"[Profile {label}] AdsPower already shows this account as Opened — "
                "waiting on debugger"
            )
            data = wait_until_profile_open(user_id, label, timeout=40.0)
            if ((data.get("ws") or {}).get("selenium") or "").strip():
                j.log(f"[Profile {label}] Account browser is open")
                return data
        j.log(f"[Profile {label}] Open click did not expose debugger — trying Start API")

    data = _start_profile_via_api(user_id, label)
    selenium_addr = ((data.get("ws") or {}).get("selenium") or "").strip()
    if not selenium_addr:
        time.sleep(3.0)
        payload = adspower_get("/api/v1/browser/active", params={"user_id": user_id})
        data = payload.get("data") or {}
        selenium_addr = ((data.get("ws") or {}).get("selenium") or "").strip()
    if not selenium_addr:
        raise RuntimeError("AdsPower did not open the account browser (no debuggerAddress)")
    j.log(f"[Profile {label}] Account browser is open — debugger {selenium_addr}")
    return data


STOP_TRIES = 3


def profile_still_open(user_id: str) -> Optional[bool]:
    """True/False from /browser/active; None when AdsPower does not answer."""
    try:
        payload = adspower_get(
            "/api/v1/browser/active", params={"user_id": user_id}, while_stopping=True
        )
    except Exception:
        return None
    info = payload.get("data") or {}
    status = str(info.get("status") or "").strip().lower()
    if status:
        return status == "active"
    return bool(((info.get("ws") or {}).get("selenium") or "").strip())


def stop_profile(user_id: str, label: str) -> None:
    """Close the profile with /api/v1/browser/stop and check it really closed."""
    j = _joiner()
    for attempt in range(1, STOP_TRIES + 1):
        try:
            adspower_get(
                "/api/v1/browser/stop", params={"user_id": user_id}, while_stopping=True
            )
        except Exception as exc:
            j.log(f"[Profile {label}] Stop API warning: {j.brief_error(exc)}")
        time.sleep(1.5 * attempt)
        still = profile_still_open(user_id)
        if still is None:
            j.log(f"[Profile {label}] Browser close not verifiable — AdsPower did not answer")
            return
        if not still:
            if attempt == 1:
                j.log(f"[Profile {label}] AdsPower stop verified — browser closed")
            else:
                j.log(f"[Profile {label}] Browser closed on stop retry {attempt}")
            return
    j.log(
        f"[Profile {label}] Browser still open after {STOP_TRIES} stop tries — "
        "close it in AdsPower"
    )
