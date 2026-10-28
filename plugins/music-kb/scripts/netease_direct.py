"""Minimal NetEase official-API client used by the fallback downloader.

Why this module exists: musicdl's NeteaseMusicClient fans out over ~30 third-party
parse mirrors per search result. Each call is bounded by s own 10s timeout, but many
mirrors are dead, so a single song can block for minutes. The official cloudsearch
API plus the eapi player-url endpoint is fast and needs no login for free tracks.

The fallback downloader only uses this module when the profile declares
source_parallel=true and lists "NeteaseDirect" in parallel_sources.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path
from typing import Any

import requests

from musicdl.modules.utils.neteaseutils import DEFAULT_COOKIES, EapiCryptoUtils

SEARCH_URL = "https://music.163.com/api/cloudsearch/pc"
EAPI_URL = "https://interface3.music.163.com/eapi/song/enhance/player/url/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134.0.0.0 Safari/537.36",
    "Referer": "https://music.163.com/",
}
FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"


def session() -> requests.Session:
    handle = requests.Session()
    handle.headers.update(HEADERS)
    handle.cookies.update(DEFAULT_COOKIES or {})
    return handle


def search(query: str, limit: int = 30, sess: requests.Session | None = None) -> list[dict[str, Any]]:
    handle = sess or session()
    response = handle.post(SEARCH_URL, data={"s": query, "type": 1, "limit": limit, "offset": 0}, timeout=20)
    response.raise_for_status()
    return ((response.json().get("result") or {}).get("songs") or [])


def resolve(song_id: Any, level: str = "standard", sess: requests.Session | None = None) -> dict[str, Any]:
    handle = sess or session()
    payload = {
        "ids": [song_id],
        "level": level,
        "encodeType": "flac",
        "header": json.dumps({
            "os": "pc",
            "appver": "",
            "osver": "",
            "deviceId": "pyncm!",
            "requestId": str(random.randrange(20000000, 30000000)),
        }),
    }
    params = EapiCryptoUtils.encryptparams(url=EAPI_URL, payload=payload)
    cookies = {"os": "pc", "appver": "", "osver": "", "deviceId": "pyncm!"}
    cookies.update(DEFAULT_COOKIES or {})
    response = handle.post(EAPI_URL, data={"params": params}, cookies=cookies, timeout=20)
    response.raise_for_status()
    data = (response.json() or {}).get("data") or [{}]
    return data[0] or {}


def download(url: str, destination: Path, sess: requests.Session | None = None) -> Path:
    handle = sess or session()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with handle.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_content(1 << 20):
                if chunk:
                    output.write(chunk)
    return destination


def duration(path: Path) -> float | None:
    probe = subprocess.run(
        [FFPROBE, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=20,
    )
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return None


def decodes(path: Path) -> tuple[bool, str | None]:
    result = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        return False, (result.stderr or "").strip()[:300]
    return True, None
