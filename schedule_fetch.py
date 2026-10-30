"""schedule_fetch: the loop a cron job (or a scheduled task) runs.

    schedule_fetch.py --root DIR [--repo PATH ...] [--log PATH] [--per-run 8] [--env-file PATH]

For every git clone directly under the root (a folder with `.git`), plus each `--repo` that lives elsewhere:
`git pull --ff-only` (before the queue check: another machine can push a clone's first `fetch-queue.md`);
when the queue holds a link, run fetch-many (`fetch.py --queue fetch-queue.md` from this folder, with this
Python, in the clone), which dedupes through `fetch-log.tsv`, writes to `raw/`, commits and pushes. A
failing clone is logged and the loop goes on. One line per clone is appended to the log. Exit 0 when
nothing failed, 1 otherwise.

So YouTube does not ban the IP: one run fetches at most `--per-run` links (8) across all clones. When
fetch-many reports a block (exit 3), the run stops before the next clone and the following runs are
skipped: 30 minutes after the first block, then 1 h, 2 h, 4 h at most, each stretched by up to a quarter
at random. A run that fetches without a block clears it. The state is `fetch-backoff.json` beside the log.

When `SUPADATA_API_KEY` is in the environment or in `--env-file`, every fetch gets it, so a YouTube block
falls back to the hosted API, plus `SUPADATA_CREDITS_LEFT`, the quota guard. At 0 fetch-many stops with
exit 4, which is not a failure and not a block. The key goes only into the child's environment, never
into argv or the log.

The guard counts every credit: at the first clone whose queue holds a YouTube link, the run reads the
balance from Supadata's `/v1/me` once (bonus credits included), then hands each fetch that balance minus
the hosted rows the run's earlier clones logged. When `/me` fails it falls back to `--hosted-quota` (100,
the free tier) minus this month's hosted rows in every clone's `fetch-log.tsv`. Either way one log line
says `hosted credits: N left of M (supadata)` or `(local count; supadata /me: <error>)`.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
PULL = ["git", "pull", "--ff-only", "-q"]
FETCH = [sys.executable, str(HERE / "fetch.py"), "--queue", "fetch-queue.md"]
QUEUE = "fetch-queue.md"
EXIT_BLOCKED = 3            # fetch-many: YouTube blocked the connection (fetch_queue.EXIT_BLOCKED)
EXIT_QUOTA = 4              # fetch-many: the hosted credits are used up (fetch_queue.EXIT_QUOTA)
HOSTED_QUOTA = 100          # hosted transcripts a month: Supadata's free tier; the fallback when /me fails
ENV_KEY = "SUPADATA_API_KEY"
ENV_CREDITS_LEFT = "SUPADATA_CREDITS_LEFT"
ENV_FILE = HERE / ".env"
LOG_NAME = "fetch-log.tsv"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}   # fetch.YOUTUBE_HOSTS
PER_RUN = 8                 # links one run fetches, across all clones
BACKOFF_FIRST_MIN = 30      # skip runs this long after the first block, doubling each time
BACKOFF_CAP_MIN = 240
BACKOFF_FILE = "fetch-backoff.json"
FETCH_TIMEOUT_S = 3 * 3600
OUTPUT_TAIL = 300

_FETCHED = re.compile(r"\bfetched (\d+), already processed")
_LINK = re.compile(r"^https?://\S+$", re.IGNORECASE)
_BULLET = re.compile(r"^[-*]\s+")

Runner = Callable[[list[str], Path, dict[str, str]], tuple[int, str]]
Balance = Callable[[str], tuple[int, int]]


def subprocess_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run one command in `cwd` with `env` added to this process's environment; (exit code, output)."""
    timeout = 300 if argv[0] == "git" else FETCH_TIMEOUT_S
    result = subprocess.run(argv, cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True,
                            timeout=timeout)
    return result.returncode, (result.stdout + result.stderr).strip()


def clones(root: Path, extra: list[Path] | tuple = ()) -> list[Path]:
    """Every git clone directly under `root`, by name, then each `extra` one not already listed."""
    found = sorted((p for p in root.iterdir() if p.is_dir() and (p / ".git").exists()), key=lambda p: p.name) \
        if root.is_dir() else []
    seen = {p.resolve() for p in found}
    for path in extra:
        if path.resolve() not in seen and (path / ".git").exists():
            found.append(path)
            seen.add(path.resolve())
    return found


def _links(queue: Path) -> list[str]:
    found = []
    for line in queue.read_text(encoding="utf-8", errors="replace").splitlines():
        candidate = _BULLET.sub("", line.strip(), count=1).strip()
        if candidate.startswith("<") and candidate.endswith(">"):
            candidate = candidate[1:-1].strip()
        if _LINK.match(candidate):
            found.append(candidate)
    return found


def has_links(queue: Path) -> bool:
    return bool(_links(queue))


def has_youtube_links(queue: Path) -> bool:
    """A YouTube link in the queue: the only kind that can spend hosted credits."""
    return queue.is_file() and any((urlparse(link).hostname or "") in YOUTUBE_HOSTS for link in _links(queue))


def _tail(output: str) -> str:
    return " ".join(output.split())[-OUTPUT_TAIL:]


def fetch_argv(limit: int) -> list[str]:
    return [*FETCH, "--max", str(limit)]


def fetched_count(output: str) -> int:
    """The links fetch-many fetched, from its summary line; 0 when it has none."""
    found = _FETCHED.findall(output)
    return int(found[-1]) if found else 0


def run_clone(path: Path, runner: Runner, env: dict[str, str], limit: int = PER_RUN,
              fetch_env: Callable[[Path], dict[str, str]] | None = None) -> tuple[bool, str, int, int]:
    """Pull and fetch one clone; (ok, what happened, fetch-many exit code or -1 when it did not run, links fetched).
    Never raises. The pull comes before the queue check: another machine can push a clone's first
    fetch-queue.md, which only the pull brings. `fetch_env`, called after the pull and only when fetch-many
    will run, adds the hosted variables to the fetch's environment."""
    try:
        code, out = runner(PULL, path, env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git pull could not run: {exc}", -1, 0
    if code != 0:
        return False, f"git pull failed (exit {code}): {_tail(out)}", -1, 0
    if not (path / QUEUE).is_file():
        return True, "pulled, no fetch-queue.md", -1, 0
    if not has_links(path / QUEUE):
        return True, "pulled, queue empty", -1, 0
    if limit <= 0:
        return True, "pulled, not fetched: this run's link limit is reached", -1, 0
    try:
        code, out = runner(fetch_argv(limit), path, {**env, **(fetch_env(path) if fetch_env else {})})
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"pulled, fetch-many could not run: {exc}", -1, 0
    if code == EXIT_QUOTA:
        return True, "pulled, hosted quota reached; YouTube links wait", code, fetched_count(out)
    if code != 0:
        return False, f"pulled, fetch-many exit {code}: {_tail(out)}", code, fetched_count(out)
    return True, "pulled, fetch-many exit 0", 0, fetched_count(out)


def hosted_used(logs: list[Path], today) -> int:
    """Hosted credits spent this month: fetched rows noted `hosted` and failed rows noted `hosted: ...`
    (a call that found no captions) in the given fetch logs. Refusals (`supadata: ...`) cost nothing."""
    month = today.isoformat()[:7]
    used = 0
    for log in logs:
        try:
            lines = log.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            cols = line.split("\t")
            if len(cols) < 6 or not cols[0].startswith(month):
                continue
            if (cols[2] == "fetched" and cols[5] == "hosted") or (cols[2] == "failed" and cols[5].startswith("hosted:")):
                used += 1
    return used


def supadata_balance(key: str) -> tuple[int, int]:
    """(credits left, credits this period) from Supadata's /v1/me; raises on any error."""
    import fetch_hosted  # only reached with a key

    return fetch_hosted.balance(fetch_hosted.make_client(key))


def read_backoff(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return {"level": int(state["level"]), "until": datetime.fromisoformat(state["until"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"level": 0, "until": None}


def backoff_minutes(level: int, stretch: float) -> float:
    """Minutes to skip after the level-th block in a row: 30, 60, 120, 240 (the cap), times `stretch`."""
    return min(BACKOFF_CAP_MIN, BACKOFF_FIRST_MIN * 2 ** (level - 1)) * stretch


def run(root: Path, *, log_path: Path, runner: Runner = subprocess_runner,
        extra: list[Path] | tuple = (), now: Callable[[], datetime] = datetime.now, per_run: int = PER_RUN,
        stretch: Callable[[], float] = lambda: random.uniform(1.0, 1.25), hosted_key: str | None = None,
        hosted_quota: int = HOSTED_QUOTA, balance: Balance = supadata_balance) -> int:
    env: dict[str, str] = {}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    backoff_path = log_path.parent / BACKOFF_FILE

    def write_log(line: str) -> None:
        with open(log_path, "a", encoding="utf-8", newline="\n") as log:
            log.write(f"{now().isoformat(timespec='seconds')} {line}\n")

    state = read_backoff(backoff_path)
    if state["until"] and now() < state["until"]:
        write_log(f"backing off after {state['level']} YouTube block(s) in a row: "
                  f"no fetch until {state['until'].isoformat(timespec='minutes')}")
        return 0

    failed = 0
    left = per_run
    fetched_clean = blocked = False
    paths = clones(root, extra)
    logs = [p / LOG_NAME for p in paths]
    guard: tuple[int, int] | None = None      # (credits left when read, hosted rows logged then)

    def read_guard(used: int) -> tuple[int, int]:
        try:
            credits, most = balance(hosted_key)
            source = "supadata"
        except Exception as exc:  # any /me failure: the local count
            credits, most = max(0, hosted_quota - used), hosted_quota
            reason = " ".join(str(exc).replace(hosted_key, "***").split()) or type(exc).__name__
            source = f"local count; supadata /me: {reason}"
        write_log(f"hosted credits: {credits} left of {most} ({source})")
        return credits, used

    def hosted_env(path: Path) -> dict[str, str]:
        nonlocal guard
        if not hosted_key:
            return {}
        used = hosted_used(logs, now().date())
        if guard is None and has_youtube_links(path / QUEUE):
            guard = read_guard(used)
        credits = max(0, guard[0] - (used - guard[1])) if guard else max(0, hosted_quota - used)
        return {ENV_KEY: hosted_key, ENV_CREDITS_LEFT: str(credits)}

    for path in paths:
        ok, what, code, fetched = run_clone(path, runner, env, left, hosted_env)
        left -= fetched
        failed += not ok
        write_log(f"{path.name}: {what}")
        if code == EXIT_BLOCKED:
            blocked = True
            break
        fetched_clean |= code in (0, 1) and fetched > 0
    if blocked:
        level = state["level"] + 1
        until = now() + timedelta(minutes=backoff_minutes(level, stretch()))
        backoff_path.write_text(json.dumps({"level": level, "until": until.isoformat(timespec="seconds")}) + "\n",
                                encoding="utf-8")
        write_log(f"YouTube blocked this connection (block {level} in a row): no fetch until "
                  f"{until.isoformat(timespec='minutes')}; the remaining clones wait")
    elif fetched_clean and backoff_path.exists():
        backoff_path.unlink()
        write_log("fetched without a block: backoff cleared")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull every clone and run fetch-many in it.")
    parser.add_argument("--root", required=True, help="folder holding the clones")
    parser.add_argument("--repo", action="append", default=[], help="a clone outside --root (repeatable)")
    parser.add_argument("--log", help="log file (default: <root>/fetch-schedule.log)")
    parser.add_argument("--per-run", type=int, default=PER_RUN, help=f"links one run fetches (default: {PER_RUN})")
    parser.add_argument("--env-file", default=str(ENV_FILE),
                        help=f"file holding {ENV_KEY} for the hosted fallback (default: .env beside this script)")
    parser.add_argument("--hosted-quota", type=int, default=HOSTED_QUOTA,
                        help=f"hosted transcripts a month when Supadata's /v1/me cannot be read (default: {HOSTED_QUOTA})")
    args = parser.parse_args(argv)
    root = Path(args.root)
    return run(root, log_path=Path(args.log) if args.log else root / "fetch-schedule.log",
               extra=[Path(r) for r in args.repo], per_run=args.per_run, hosted_key=read_key(Path(args.env_file)),
               hosted_quota=args.hosted_quota)


def read_key(env_file: Path) -> str | None:
    """The hosted API key from the environment, else from `env_file`; None without one."""
    if os.environ.get(ENV_KEY):
        return os.environ[ENV_KEY]
    from fetch_hosted import read_env_file

    return read_env_file(env_file).get(ENV_KEY) or None


if __name__ == "__main__":
    sys.exit(main())
