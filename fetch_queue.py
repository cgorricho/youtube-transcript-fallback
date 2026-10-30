"""fetch-many: fetch every new YouTube link in a queue file.

The queue (`fetch-queue.md`) holds one link per line; `fetch-log.tsv` beside it records
every attempt, so every machine that fetches into the same git repository shares what was
already fetched. See README.md.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from fetch import FetchError, video_id_from_url

LOG_HEADER = "date\tvideo_id\tresult\traw_file\turl\tnote"
MAX_CONSECUTIVE_FAILURES = 3
BLOCKED = ("IpBlocked", "RequestBlocked")   # YouTube refusing this connection: stop at once
HOSTED_STOP = "HostedStop"  # the hosted API refusing the key: stop like a block
HOSTED_QUOTA = "HostedQuota"  # hosted credits used up: stop, log nothing
HOSTED_NOTE = "hosted"      # fetch-log note of a fetch through the hosted API; the quota guard counts it
PAUSE_GROWTH = 1.25     # each pause is this much longer than the one before
PAUSE_CAP = 180.0       # seconds; the growth stops here
EXIT_BLOCKED = 3
EXIT_QUOTA = 4

_BULLET = re.compile(r"^[-*]\s+")
_LINK = re.compile(r"^https?://\S+$", re.IGNORECASE)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class QueueLine:
    text: str                   # the line as written, kept verbatim on rewrite
    kind: str                   # blank | comment | youtube | junk
    url: str | None = None
    video_id: str | None = None


@dataclass(frozen=True)
class LogEntry:
    date: date
    video_id: str
    result: str                 # fetched | duplicate | failed
    raw_file: str
    url: str
    note: str


@dataclass
class QueueResult:
    exit_code: int = 0
    fetched: int = 0
    duplicates: int = 0
    failed: int = 0
    left: int = 0
    stopped: bool = False
    blocked: bool = False
    quota: bool = False


def parse_line(text: str) -> QueueLine:
    stripped = text.strip()
    if not stripped:
        return QueueLine(text, "blank")
    if stripped.startswith("#"):
        return QueueLine(text, "comment")
    candidate = _BULLET.sub("", stripped, count=1).strip()
    if candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1].strip()
    if not _LINK.match(candidate):
        return QueueLine(text, "junk")
    try:
        return QueueLine(text, "youtube", candidate, video_id_from_url(candidate))
    except FetchError:  # another site, or a YouTube URL without a video: kept, not fetched
        return QueueLine(text, "junk")


def _field(value: str) -> str:
    return re.sub(r"[\t\r\n]", " ", value)


def append_log(path: Path, entry: LogEntry) -> None:
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        if new:
            handle.write(LOG_HEADER + "\n")
        fields = [entry.date.isoformat(), entry.video_id, entry.result,
                  entry.raw_file, entry.url, entry.note]
        handle.write("\t".join(_field(f) for f in fields) + "\n")


def _fetched_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line == LOG_HEADER:
            continue
        cols = line.split("\t")
        if len(cols) >= 3 and cols[2] == "fetched":
            rows.append(cols + [""] * (6 - len(cols)))
    return rows


def read_log(path: Path) -> dict[str, str]:
    """Video IDs logged as fetched, mapped to their raw filename."""
    return {cols[1]: cols[3] for cols in _fetched_rows(path) if cols[1]}


def _scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return value.strip('"')
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    return value


def _frontmatter(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 3)
    return text[4:end + 1] if end != -1 else None


def scan_raw(raw_dir: Path) -> dict[str, str]:
    """Video IDs found as `video_id` in raw/*.md frontmatter, mapped to the file's title."""
    found: dict[str, str] = {}
    for path in sorted(raw_dir.glob("*.md")):
        try:
            head = _frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if head is None:
            continue
        vid_match = re.search(r"^video_id:(.*)$", head, re.MULTILINE)
        if not vid_match:
            continue
        vid = _scalar(vid_match.group(1))
        if not _VIDEO_ID.match(vid):
            continue
        title_match = re.search(r"^title:(.*)$", head, re.MULTILINE)
        title = _scalar(title_match.group(1)) if title_match else ""
        found[vid] = title or path.name
    return found


def commit_message(videos: int) -> str:
    return f"fetch: {videos} video" if videos == 1 else f"fetch: {videos} videos"


def git_commit_and_push(paths: Iterable[Path], message: str) -> None:
    """Stage only the given paths, commit them, push. A failed push keeps the commit."""
    names = [str(p) for p in paths]

    def git(*args: str):
        return subprocess.run(["git", *args], capture_output=True, text=True)

    for args in (("add", "--", *names), ("commit", "-m", message, "--", *names)):
        done = git(*args)
        if done.returncode != 0:
            print(f"git {args[0]} failed: {done.stderr.strip()}")
            return
    done = git("push")
    if done.returncode != 0:
        print(f"git push failed: {done.stderr.strip()}")
        print("the commit is kept; the next run pushes it")


def is_hosted_file(path: Path) -> bool:
    """A raw file written by the hosted fallback: its frontmatter says so."""
    try:
        head = _frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    return head is not None and re.search(r"^transcription:\s*\"?supadata-", head, re.MULTILINE) is not None


def _describe(exc: Exception) -> str:
    if isinstance(exc, FetchError):
        return str(exc)
    name = type(exc).__name__
    if name in BLOCKED:
        return f"{name}: YouTube blocked the request"
    return f"{name}: {exc}".strip()


def run_queue(
    queue_path: Path,
    raw_dir: Path,
    log_path: Path,
    fetcher: Callable[[str], Path],
    *,
    today: date,
    pause: float = 10,
    growth: float = PAUSE_GROWTH,
    jitter: Callable[[], float] = lambda: random.uniform(0.75, 1.25),
    max_new: int | None = None,
    push: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    git_step: Callable[[list[Path], str], None] = git_commit_and_push,
) -> QueueResult:
    if not queue_path.is_file():
        raise FetchError(f"queue file not found: {queue_path}", 2)
    if not raw_dir.is_dir():
        raise FetchError(f"output directory does not exist: {raw_dir}", 2)

    text = queue_path.read_text(encoding="utf-8")
    lines = [parse_line(line) for line in text.splitlines()]
    if any(line.kind == "youtube" for line in lines):
        print(f"fetch: {queue_path} is rewritten at the end; close it in your editor or stop typing until done")

    logged = read_log(log_path)
    titles = scan_raw(raw_dir)
    known: dict[str, tuple[str, str]] = {}   # video_id -> (label, raw_file)
    for vid, raw_file in logged.items():
        known[vid] = (raw_file or vid, raw_file)
    for vid, title in titles.items():
        known[vid] = (title, known.get(vid, ("", ""))[1])

    result = QueueResult()
    keep = [True] * len(lines)
    seen: set[str] = set()
    new_files: list[Path] = []
    attempts = 0
    consecutive = 0
    logged_any = False

    def log(entry: LogEntry) -> None:
        nonlocal logged_any
        append_log(log_path, entry)
        logged_any = True

    for i, line in enumerate(lines):
        if line.kind == "junk":
            print(f"not a YouTube video link: {line.text.strip()}")
            continue
        if line.kind != "youtube" or result.stopped:
            continue
        url = line.url
        vid = key = line.video_id or ""
        if key in known:
            label, raw_file = known[key]
            log(LogEntry(today, vid, "duplicate", raw_file, url, ""))
            print(f"already processed: {label}")
            result.duplicates += 1
            keep[i] = False
            continue
        if key in seen:
            print(f"duplicate in queue: {url}")
            keep[i] = False
            continue
        seen.add(key)
        if max_new is not None and attempts >= max_new:
            continue
        if attempts:  # a pause that grows with each attempt, uneven so the run does not look scripted 
            sleep(round(min(PAUSE_CAP, pause * growth ** (attempts - 1)) * jitter(), 1))
        attempts += 1
        try:
            path = fetcher(url)
        except Exception as exc:  # one bad video must not end the run
            if type(exc).__name__ == HOSTED_QUOTA:
                result.stopped = result.quota = True
                print(f"stopped: {exc}; the rest stays in the queue")
                continue
            reason = _describe(exc)
            log(LogEntry(today, vid, "failed", "", url, reason))
            print(f"failed: {url}: {reason}")
            result.failed += 1
            consecutive += 1
            if type(exc).__name__ in BLOCKED:
                result.stopped = result.blocked = True
                print("stopped: YouTube is blocking this connection; the rest stays in the queue")
            elif type(exc).__name__ == HOSTED_STOP:
                result.stopped = result.blocked = True
                print("stopped: the hosted transcript API refused the key; the rest stays in the queue")
            elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                result.stopped = True
                print(f"stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures; "
                      "YouTube may be blocking this connection")
            continue
        consecutive = 0
        path = Path(path)
        log(LogEntry(today, vid, "fetched", path.name, url, HOSTED_NOTE if is_hosted_file(path) else ""))
        print(f"fetched: {path}")
        known[key] = (path.name, path.name)
        new_files.append(path)
        result.fetched += 1
        keep[i] = False

    if not all(keep):
        kept = [line.text for line, k in zip(lines, keep) if k]
        body = "\n".join(kept)
        if kept and text.endswith(("\n", "\r")):
            body += "\n"
        with open(queue_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)

    result.left = sum(
        1 for line, k in zip(lines, keep) if k and line.kind not in ("blank", "comment")
    )
    if result.blocked:
        result.exit_code = EXIT_BLOCKED
    elif result.quota:
        result.exit_code = EXIT_QUOTA
    elif result.stopped:
        result.exit_code = 2
    elif result.failed:
        result.exit_code = 1

    if push and (logged_any or not all(keep)):
        message = commit_message(result.fetched)
        try:
            git_step([p for p in (*new_files, queue_path, log_path) if p.exists()], message)
        except Exception as exc:
            print(f"git step failed: {_describe(exc)}")

    print(f"fetched {result.fetched}, already processed {result.duplicates}, "
          f"failed {result.failed}, left in queue {result.left}")
    return result
