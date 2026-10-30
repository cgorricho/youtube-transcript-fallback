"""Hosted transcript fallback: Supadata, through its official Python SDK.

Used only when YouTube blocks the local fetch (a datacenter IP, say) and `SUPADATA_API_KEY` is set,
in the environment or in the `.env` beside this file. Existing captions only (`mode=native`): a video
without captions fails instead of spending AI-transcript credits. `balance` reads the credits left from
`/v1/me` for schedule_fetch's quota guard.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests
from supadata import Supadata, SupadataError
from supadata.types import BatchJob

from fetch import ACCEPTED_LANGUAGES, FetchError, Fragment, base_language, canonical_url

ENV_KEY = "SUPADATA_API_KEY"
ENV_CREDITS_LEFT = "SUPADATA_CREDITS_LEFT"      # set by schedule_fetch from the monthly quota guard
ENV_FILE = Path(__file__).resolve().parent / ".env"
ENV_FILES = (ENV_FILE,)
POLL_EVERY_S = 2
POLL_LIMIT_S = 600
STOP_CODES = {"unauthorized", "forbidden", "upgrade-required"}
QUOTA_CODES = {"limit-exceeded"}                # out of credits: links wait, no backoff
STOP_STATUSES = {401, 403, 429}
spent = 0                                       # hosted calls made by this process (the quota guard)
OEMBED = "https://www.youtube.com/oembed?url={url}&format=json"


class HostedStop(FetchError):
    """The key is refused: stop the run like a YouTube block (exit 3)."""

    def __init__(self, message: str):
        super().__init__(message, 3)


class HostedQuota(FetchError):
    """The hosted credits are used up (the guard in schedule_fetch, or the API's `limit-exceeded`):
    stop with exit 4, log nothing, the links wait."""

    def __init__(self, message: str):
        super().__init__(message, 4)


@dataclass(frozen=True)
class HostedSettings:
    key: str = field(repr=False)
    credits_left: int | None = None     # None: no guard (a hand run)


@dataclass(frozen=True)
class HostedTranscript:
    lang: str
    fragments: list[Fragment]
    duration: int                       # seconds, from the last segment's end


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines of a .env file; comments, blanks and malformed lines skipped."""
    found: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return found
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        if not name.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        found[name] = value
    return found


def settings_from_env(env, env_files=ENV_FILES) -> HostedSettings | None:
    """The key from `env`, else from the first of `env_files` that holds it; None without one."""
    key = env.get(ENV_KEY) or next((k for f in env_files if (k := read_env_file(f).get(ENV_KEY))), None)
    if not key:
        return None
    try:
        left = int(env[ENV_CREDITS_LEFT]) if ENV_CREDITS_LEFT in env else None
    except ValueError:
        left = None
    return HostedSettings(key, left)


def make_client(key: str) -> Supadata:
    return Supadata(api_key=key)


def _chunk(chunk, name: str):
    return chunk.get(name) if isinstance(chunk, dict) else getattr(chunk, name, None)


def _map(content, lang: str) -> HostedTranscript:
    if isinstance(content, str):
        return HostedTranscript(lang, [Fragment(content, 0.0)], 0)
    fragments: list[Fragment] = []
    end_ms = 0
    for chunk in content or []:
        offset = int(_chunk(chunk, "offset") or 0)
        fragments.append(Fragment(_chunk(chunk, "text") or "", offset / 1000))
        end_ms = max(end_ms, offset + int(_chunk(chunk, "duration") or 0))
    return HostedTranscript(lang, fragments, round(end_ms / 1000))


def _http_cause(exc: BaseException) -> requests.HTTPError | None:
    """The HTTP error the SDK was handling when `exc` arose: an error body it cannot turn into a
    SupadataError (a field missing or extra) raises TypeError inside its `except HTTPError`."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, requests.HTTPError):
            return exc
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return None


def _sdk_error(exc: Exception) -> FetchError:
    if not isinstance(exc, (SupadataError, requests.HTTPError)):
        exc = _http_cause(exc) or exc
    if isinstance(exc, SupadataError):
        if exc.error in STOP_CODES:
            return HostedStop(f"supadata: {exc.error}: {exc.message}")
        if exc.error in QUOTA_CODES:
            return HostedQuota(f"supadata: {exc.error}: {exc.message}")
        if exc.error == "transcript-unavailable":
            return FetchError("hosted: no captions on this video", 1)
        return FetchError(f"hosted: {exc.message} ({exc.error})", 1)
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in STOP_STATUSES:
            return HostedStop(f"supadata: HTTP {status}")
        return FetchError(f"hosted: HTTP {status}", 1)
    return FetchError(f"hosted: {type(exc).__name__}: {exc}".strip(), 1)


def balance(client) -> tuple[int, int]:
    """(credits left, credits this period) from `/v1/me`; the SDK has no call for it, its request
    helper does it. Any error (network, HTTP, a missing field) is a FetchError; the message never holds
    the key or the organization id."""
    try:
        me = client._request("GET", "/me")
        most, used = int(me["max_credits"]), int(me["used_credits"])
    except Exception as exc:  # SDK, HTTP, network, or an unexpected response shape
        raise FetchError(_me_error(exc), 1) from None
    return max(0, most - used), most


def _me_error(exc: Exception) -> str:
    if not isinstance(exc, (SupadataError, requests.HTTPError)):
        exc = _http_cause(exc) or exc
    if isinstance(exc, SupadataError):
        return f"{exc.error}: {exc.message}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "no maxCredits or usedCredits in the answer"
    return f"{type(exc).__name__}: {exc}".strip()


def _wait_for_job(client, job_id: str, sleep, clock) -> tuple[object, str]:
    # The SDK has no public call for the transcript job endpoint; its own request helper does it.
    started = clock()
    while True:
        job = client._request("GET", f"/transcript/{job_id}")
        status = job.get("status")
        if status == "completed":
            return job.get("content"), job.get("lang") or ""
        if status == "failed":
            raise FetchError(f"hosted: transcript job failed: {job.get('error') or 'no reason given'}", 1)
        if clock() - started >= POLL_LIMIT_S:
            raise FetchError(f"hosted: transcript job {job_id} still running after {POLL_LIMIT_S} s", 1)
        sleep(POLL_EVERY_S)


def _ask(client, video_id: str, lang: str | None, sleep, clock):
    try:
        answer = client.transcript(canonical_url(video_id), lang=lang, text=False, mode="native")
        if isinstance(answer, BatchJob):
            content, got = _wait_for_job(client, answer.job_id, sleep, clock)
            return content, got, [got]
        return answer.content, answer.lang or "", list(answer.available_langs or [])
    except FetchError:
        raise
    except Exception as exc:  # SDK, HTTP, network, or an unexpected response shape
        raise _sdk_error(exc) from None


def hosted_transcript(video_id: str, client, *, sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic) -> HostedTranscript:
    """Network call through `client` (the SDK's Supadata, or a fake). en or es captions only."""
    content, lang, available = _ask(client, video_id, None, sleep, clock)
    if base_language(lang) not in ACCEPTED_LANGUAGES:
        wanted = next((c for c in ACCEPTED_LANGUAGES if c in {base_language(a) for a in available}), None)
        if wanted is None:
            raise FetchError(f"hosted: no en or es captions; available: {', '.join(available) or 'none'}", 1)
        content, lang, _ = _ask(client, video_id, wanted, sleep, clock)
    return _map(content, lang)


def urllib_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (youtube-transcript-fallback)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def oembed(video_id: str, http: Callable[[str], dict] = urllib_json) -> dict:
    """Title and channel from YouTube's oEmbed, which answers even a blocked IP; {} when it fails."""
    url = OEMBED.format(url=urllib.parse.quote(canonical_url(video_id), safe=""))
    try:
        data = http(url)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
