from datetime import date
from pathlib import Path

import pytest

import fetch
import fetch_queue
from fetch import FetchError
from fetch_queue import (
    LOG_HEADER,
    LogEntry,
    append_log,
    parse_line,
    read_log,
    run_queue,
    scan_raw,
)

TODAY = date(2026, 9, 30)
ID_A = "zjkBMFhNj_g"
ID_B = "aaaaaaaaaaa"
ID_C = "bbbbbbbbbbb"
ID_D = "ccccccccccc"
ID_E = "ddddddddddd"


# --- parsing -----------------------------------------------------------------


@pytest.mark.parametrize("text", [
    f"https://www.youtube.com/watch?v={ID_A}",
    f"https://youtube.com/watch?v={ID_A}&t=120s",
    f"https://www.youtube.com/watch?list=PL1&v={ID_A}&index=3",
    f"https://youtu.be/{ID_A}",
    f"https://youtu.be/{ID_A}?si=abc&t=5",
    f"https://www.youtube.com/shorts/{ID_A}",
    f"- https://youtu.be/{ID_A}",
    f"* https://youtu.be/{ID_A}",
    f"<https://youtu.be/{ID_A}>",
    f"- <https://youtu.be/{ID_A}>",
    f"   https://youtu.be/{ID_A}   ",
])
def test_parse_line_youtube_shapes(text):
    line = parse_line(text)
    assert line.kind == "youtube"
    assert line.video_id == ID_A
    assert line.text == text
    assert line.url.startswith("https://")


@pytest.mark.parametrize("text,kind", [
    ("", "blank"),
    ("   ", "blank"),
    ("# to watch", "comment"),
    ("## later", "comment"),
    ("https://example.com/article", "junk"),
    ("- https://vimeo.com/12345", "junk"),
    ("https://example.com/papers/a.pdf", "junk"),
    ("remember to check this", "junk"),
    ("- some note", "junk"),
    ("https://www.youtube.com/channel/UCabc", "junk"),
])
def test_parse_line_other_kinds(text, kind):
    line = parse_line(text)
    assert line.kind == kind
    assert line.video_id is None


# --- log ---------------------------------------------------------------------


def test_append_log_writes_header_once_and_rows(tmp_path):
    log = tmp_path / "fetch-log.tsv"
    append_log(log, LogEntry(TODAY, ID_A, "fetched", "2023-11-22-x.md", "https://youtu.be/x", ""))
    append_log(log, LogEntry(TODAY, ID_B, "failed", "", "https://youtu.be/y", "no\tusable\ncaptions"))
    lines = log.read_bytes().decode().split("\n")
    assert lines[0] == LOG_HEADER
    assert lines[1] == f"2026-09-30\t{ID_A}\tfetched\t2023-11-22-x.md\thttps://youtu.be/x\t"
    assert lines[2] == f"2026-09-30\t{ID_B}\tfailed\t\thttps://youtu.be/y\tno usable captions"
    assert lines[3] == ""
    assert b"\r" not in log.read_bytes()


def test_read_log_returns_only_fetched_ids(tmp_path):
    log = tmp_path / "fetch-log.tsv"
    append_log(log, LogEntry(TODAY, ID_A, "fetched", "a.md", "u", ""))
    append_log(log, LogEntry(TODAY, ID_B, "failed", "", "u", "boom"))
    append_log(log, LogEntry(TODAY, ID_C, "duplicate", "c.md", "u", ""))
    assert read_log(log) == {ID_A: "a.md"}


def test_read_log_missing_file_is_empty(tmp_path):
    assert read_log(tmp_path / "missing.tsv") == {}


# --- raw scan ----------------------------------------------------------------


def _raw(raw_dir: Path, name: str, frontmatter: str, body: str = "body\n") -> None:
    (raw_dir / name).write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_scan_raw_finds_quoted_and_bare_ids(tmp_path):
    _raw(tmp_path, "a.md", f'title: "Intro \\"LLMs\\""\nvideo_id: "{ID_A}"\n')
    _raw(tmp_path, "b.md", f"title: Plain title\nvideo_id: {ID_B}\n")
    _raw(tmp_path, "c.md", "title: Web clip\nsource: https://example.com\n")
    (tmp_path / "d.md").write_text(f"no frontmatter\nvideo_id: {ID_C}\n", encoding="utf-8")
    _raw(tmp_path, "e.md", "title: body mention\n", body=f"video_id: {ID_D}\n")
    assert scan_raw(tmp_path) == {ID_A: 'Intro "LLMs"', ID_B: "Plain title"}


def test_scan_raw_real_render_document_output(tmp_path):
    meta = fetch.Metadata("T", "C", date(2023, 1, 1), 60, "", "u", "en", ID_A)
    doc = fetch.render_document(meta, fetch.Track("en", False), [], TODAY)
    (tmp_path / "x.md").write_text(doc, encoding="utf-8")
    assert scan_raw(tmp_path) == {ID_A: "T"}


# --- run loop ----------------------------------------------------------------


class FakeFetcher:
    """Writes a raw file for each URL unless its video ID is in fail."""

    def __init__(self, raw_dir: Path, fail=None):
        self.raw_dir = raw_dir
        self.fail = dict(fail or {})
        self.calls: list[str] = []

    def __call__(self, url: str) -> Path:
        self.calls.append(url)
        vid = fetch.video_id_from_url(url)
        if vid in self.fail:
            raise self.fail[vid]
        path = self.raw_dir / f"2026-01-01-{vid.lower()}.md"
        path.write_text(f'---\ntitle: "Video {vid}"\nvideo_id: "{vid}"\n---\n', encoding="utf-8")
        return path


class Env:
    def __init__(self, tmp_path: Path, queue_text: str):
        self.root = tmp_path
        self.raw = tmp_path / "raw"
        self.raw.mkdir()
        self.queue = tmp_path / "fetch-queue.md"
        self.queue.write_text(queue_text, encoding="utf-8")
        self.log = tmp_path / "fetch-log.tsv"
        self.sleeps: list[float] = []
        self.git_calls: list[tuple[list[Path], str]] = []

    def run(self, fetcher, **kwargs):
        kwargs.setdefault("pause", 5)
        kwargs.setdefault("jitter", lambda: 1.0)
        kwargs.setdefault("push", True)
        return run_queue(
            self.queue, self.raw, self.log, fetcher, today=TODAY,
            sleep=self.sleeps.append,
            git_step=lambda paths, message: self.git_calls.append((list(paths), message)),
            **kwargs,
        )

    def log_rows(self):
        return [line.split("\t") for line in self.log.read_text(encoding="utf-8").splitlines()[1:]]


def yt(vid):
    return f"https://youtu.be/{vid}"


def test_fetches_new_links_and_rewrites_queue_keeping_comments_and_order(tmp_path, capsys):
    queue = (
        "# Queue\n"
        "\n"
        f"- {yt(ID_A)}\n"
        "https://example.com/article\n"
        "a stray note\n"
        f"<{yt(ID_B)}>\n"
        "\n"
        "# end\n"
    )
    env = Env(tmp_path, queue)
    fetcher = FakeFetcher(env.raw)
    result = env.run(fetcher)
    assert result.exit_code == 0
    assert fetcher.calls == [yt(ID_A), yt(ID_B)]
    assert env.queue.read_text(encoding="utf-8") == (
        "# Queue\n"
        "\n"
        "https://example.com/article\n"
        "a stray note\n"
        "\n"
        "# end\n"
    )
    rows = env.log_rows()
    assert [(r[1], r[2], r[3]) for r in rows] == [
        (ID_A, "fetched", f"2026-01-01-{ID_A.lower()}.md"),
        (ID_B, "fetched", f"2026-01-01-{ID_B.lower()}.md"),
    ]
    assert env.sleeps == [5]  # a pause before each attempt after the first
    out = capsys.readouterr().out
    assert "not a YouTube video link: https://example.com/article" in out
    assert "not a YouTube video link: a stray note" in out
    assert "fetched 2, already processed 0, failed 0, left in queue 2" in out
    assert (result.fetched, result.duplicates, result.failed, result.left) == (2, 0, 0, 2)


def test_already_processed_from_log_and_raw_frontmatter(tmp_path, capsys):
    env = Env(tmp_path, f"{yt(ID_A)}\n{yt(ID_B)}\n{yt(ID_C)}\n")
    append_log(env.log, LogEntry(TODAY, ID_A, "fetched", "old-a.md", yt(ID_A), ""))
    _raw(env.raw, "2020-01-01-b.md", f'title: "Old B"\nvideo_id: "{ID_B}"\n')
    fetcher = FakeFetcher(env.raw)
    result = env.run(fetcher)
    assert fetcher.calls == [yt(ID_C)]
    assert env.queue.read_text(encoding="utf-8") == ""
    rows = env.log_rows()
    assert [(r[1], r[2]) for r in rows] == [
        (ID_A, "fetched"), (ID_A, "duplicate"), (ID_B, "duplicate"), (ID_C, "fetched"),
    ]
    out = capsys.readouterr().out
    assert "already processed: old-a.md" in out
    assert "already processed: Old B" in out
    assert (result.fetched, result.duplicates, result.exit_code) == (1, 2, 0)


def test_failed_log_rows_do_not_count_as_processed(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n")
    append_log(env.log, LogEntry(TODAY, ID_A, "failed", "", yt(ID_A), "blocked"))
    fetcher = FakeFetcher(env.raw)
    env.run(fetcher)
    assert fetcher.calls == [yt(ID_A)]


def test_duplicates_within_queue_fetched_once(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\nhttps://www.youtube.com/watch?v={ID_A}&t=3\n")
    fetcher = FakeFetcher(env.raw)
    result = env.run(fetcher)
    assert fetcher.calls == [yt(ID_A)]
    assert env.queue.read_text(encoding="utf-8") == ""
    assert result.exit_code == 0


def test_duplicate_of_a_failed_link_keeps_only_the_first_line(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n{yt(ID_A)}?si=x\n")
    fetcher = FakeFetcher(env.raw, fail={ID_A: FetchError("no usable captions; available: none")})
    result = env.run(fetcher)
    assert fetcher.calls == [yt(ID_A)]
    assert env.queue.read_text(encoding="utf-8") == f"{yt(ID_A)}\n"
    assert result.exit_code == 1


def test_failure_logged_line_kept_exit_1(tmp_path, capsys):
    env = Env(tmp_path, f"{yt(ID_A)}\n{yt(ID_B)}\n")
    fetcher = FakeFetcher(env.raw, fail={ID_A: FetchError("no usable captions; available: fr (manual)")})
    result = env.run(fetcher)
    assert result.exit_code == 1
    assert env.queue.read_text(encoding="utf-8") == f"{yt(ID_A)}\n"
    rows = env.log_rows()
    assert rows[0][1:3] == [ID_A, "failed"]
    assert rows[0][5] == "no usable captions; available: fr (manual)"
    assert "failed: " in capsys.readouterr().out


def test_unexpected_exception_is_a_failure_not_a_crash(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n")
    fetcher = FakeFetcher(env.raw, fail={ID_A: ConnectionError("reset by peer")})
    result = env.run(fetcher)
    assert result.exit_code == 1
    assert env.log_rows()[0][5] == "ConnectionError: reset by peer"


def test_stops_after_three_consecutive_failures(tmp_path, capsys):
    queue = "".join(f"{yt(v)}\n" for v in (ID_A, ID_B, ID_C, ID_D, ID_E))
    env = Env(tmp_path, queue)
    boom = FetchError("blocked")
    fetcher = FakeFetcher(env.raw, fail={ID_A: boom, ID_B: boom, ID_C: boom})
    result = env.run(fetcher)
    assert result.exit_code == 2
    assert result.stopped
    assert fetcher.calls == [yt(ID_A), yt(ID_B), yt(ID_C)]
    assert env.queue.read_text(encoding="utf-8") == queue
    assert len(env.log_rows()) == 3
    assert "stopped after 3 consecutive failures" in capsys.readouterr().out


def test_success_resets_the_failure_count(tmp_path):
    queue = "".join(f"{yt(v)}\n" for v in (ID_A, ID_B, ID_C, ID_D, ID_E))
    env = Env(tmp_path, queue)
    boom = FetchError("x")
    fetcher = FakeFetcher(env.raw, fail={ID_A: boom, ID_B: boom, ID_D: boom, ID_E: boom})
    result = env.run(fetcher)
    assert len(fetcher.calls) == 5
    assert result.exit_code == 1


def test_max_limits_new_videos_and_leaves_the_rest(tmp_path):
    queue = f"{yt(ID_A)}\n{yt(ID_B)}\n{yt(ID_C)}\n"
    env = Env(tmp_path, queue)
    append_log(env.log, LogEntry(TODAY, ID_A, "fetched", "a.md", yt(ID_A), ""))
    fetcher = FakeFetcher(env.raw)
    result = env.run(fetcher, max_new=1)
    assert fetcher.calls == [yt(ID_B)]
    assert env.queue.read_text(encoding="utf-8") == f"{yt(ID_C)}\n"
    assert (result.fetched, result.duplicates, result.left) == (1, 1, 1)


def test_pause_value_is_used(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n{yt(ID_B)}\n{yt(ID_C)}\n")
    env.run(FakeFetcher(env.raw), pause=0.5)
    assert env.sleeps == [0.5, 0.6]


def test_pauses_grow_to_the_cap_and_vary_with_the_jitter(tmp_path):
    ids = [f"{c}" * 11 for c in "abcdefghijklmnopqrstuvwxyz"[:12]]
    env = Env(tmp_path, "".join(f"{yt(v)}\n" for v in ids))
    env.run(FakeFetcher(env.raw), pause=60, jitter=lambda: 1.2)
    assert env.sleeps[:3] == [72.0, 90.0, 112.5]
    assert env.sleeps[-1] == 216.0  # 180 s cap, times the jitter
    assert len(env.sleeps) == 11


class IpBlocked(Exception):
    """Stands in for youtube_transcript_api's IpBlocked: the check is by class name."""


def test_a_youtube_block_stops_the_run_at_once_with_exit_3(tmp_path, capsys):
    queue = "".join(f"{yt(v)}\n" for v in (ID_A, ID_B, ID_C, ID_D))
    env = Env(tmp_path, queue)
    fetcher = FakeFetcher(env.raw, fail={ID_B: IpBlocked("blocked")})
    result = env.run(fetcher)
    assert fetcher.calls == [yt(ID_A), yt(ID_B)]
    assert (result.exit_code, result.blocked, result.stopped) == (3, True, True)
    assert env.queue.read_text(encoding="utf-8") == "".join(f"{yt(v)}\n" for v in (ID_B, ID_C, ID_D))
    assert env.log_rows()[-1][5] == "IpBlocked: YouTube blocked the request"
    out = capsys.readouterr().out
    assert "stopped: YouTube is blocking this connection" in out
    assert "fetched 1, already processed 0, failed 1, left in queue 3" in out


def test_git_step_stages_new_raw_files_queue_and_log(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n{yt(ID_B)}\n")
    fetcher = FakeFetcher(env.raw)
    env.run(fetcher)
    assert len(env.git_calls) == 1
    paths, message = env.git_calls[0]
    assert message == "fetch: 2 videos"
    assert set(paths) == {
        env.raw / f"2026-01-01-{ID_A.lower()}.md",
        env.raw / f"2026-01-01-{ID_B.lower()}.md",
        env.queue,
        env.log,
    }


def test_git_step_singular_message(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n")
    env.run(FakeFetcher(env.raw))
    assert env.git_calls[0][1] == "fetch: 1 video"


def test_no_push_skips_git_step(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n")
    env.run(FakeFetcher(env.raw), push=False)
    assert env.git_calls == []


def test_empty_queue_exit_0_no_git_and_no_rewrite(tmp_path, capsys):
    env = Env(tmp_path, "# nothing yet\n\n")
    before = env.queue.stat().st_mtime_ns
    result = env.run(FakeFetcher(env.raw))
    assert result.exit_code == 0
    assert env.git_calls == []
    assert env.queue.stat().st_mtime_ns == before
    assert not env.log.exists()
    assert "fetched 0, already processed 0, failed 0, left in queue 0" in capsys.readouterr().out


def test_queue_without_trailing_newline_is_preserved(tmp_path):
    env = Env(tmp_path, f"# q\n{yt(ID_A)}\nnote")
    env.run(FakeFetcher(env.raw))
    assert env.queue.read_text(encoding="utf-8") == "# q\nnote"


def test_crlf_queue_is_rewritten_with_lf(tmp_path):
    env = Env(tmp_path, "")
    env.queue.write_bytes(f"# q\r\n{yt(ID_A)}\r\nnote\r\n".encode())
    env.run(FakeFetcher(env.raw))
    assert env.queue.read_bytes() == b"# q\nnote\n"


def test_warns_about_the_queue_being_rewritten(tmp_path, capsys):
    env = Env(tmp_path, f"{yt(ID_A)}\n")
    env.run(FakeFetcher(env.raw))
    assert "close it in your editor" in capsys.readouterr().out


# --- git step ----------------------------------------------------------------


def test_git_commit_and_push_runs_add_commit_push(monkeypatch, tmp_path):
    calls = []

    class Done:
        def __init__(self, code):
            self.returncode = code
            self.stderr = "err"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Done(0)

    monkeypatch.setattr(fetch_queue.subprocess, "run", fake_run)
    fetch_queue.git_commit_and_push([tmp_path / "a.md", tmp_path / "q.md"], "fetch: 1 video")
    assert calls[0][:3] == ["git", "add", "--"]
    assert calls[1][:4] == ["git", "commit", "-m", "fetch: 1 video"]
    assert "--" in calls[1] and str(tmp_path / "q.md") in calls[1]
    assert calls[2] == ["git", "push"]


def test_git_push_failure_warns_and_keeps_commit(monkeypatch, tmp_path, capsys):
    class Done:
        def __init__(self, code):
            self.returncode = code
            self.stderr = "rejected"

    def fake_run(cmd, **kwargs):
        return Done(1 if cmd[:2] == ["git", "push"] else 0)

    monkeypatch.setattr(fetch_queue.subprocess, "run", fake_run)
    assert fetch_queue.git_commit_and_push([tmp_path / "a.md"], "fetch: 1 video") is None
    out = capsys.readouterr().out
    assert "push failed" in out
    assert "commit is kept" in out


# --- CLI ---------------------------------------------------------------------


def test_main_queue_missing_exits_2(tmp_path, capsys):
    code = fetch.main(["--queue", str(tmp_path / "nope.md"), "--out", str(tmp_path)])
    assert code == 2
    assert "queue file not found" in capsys.readouterr().err


def test_main_raw_dir_missing_exits_2(tmp_path, capsys):
    (tmp_path / "q.md").write_text("", encoding="utf-8")
    code = fetch.main(["--queue", str(tmp_path / "q.md"), "--out", str(tmp_path / "raw")])
    assert code == 2
    assert "output directory does not exist" in capsys.readouterr().err


def test_main_requires_url_or_queue_not_both(tmp_path):
    with pytest.raises(SystemExit) as exc:
        fetch.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        fetch.main([yt(ID_A), "--queue", "q.md"])
    assert exc.value.code == 2


def test_main_queue_wires_options(monkeypatch, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    queue = tmp_path / "fetch-queue.md"
    queue.write_text(f"{yt(ID_A)}\n", encoding="utf-8")
    seen = {}

    def fake_run_queue(queue_path, raw_dir, log_path, fetcher, **kwargs):
        seen.update(queue=queue_path, raw=raw_dir, log=log_path, **kwargs)
        return fetch_queue.QueueResult(exit_code=1)

    monkeypatch.setattr(fetch_queue, "run_queue", fake_run_queue)
    code = fetch.main(["--queue", str(queue), "--out", str(raw), "--pause", "2", "--max", "3", "--no-push"])
    assert code == 1
    assert seen["queue"] == queue
    assert seen["raw"] == raw
    assert seen["log"] == tmp_path / "fetch-log.tsv"
    assert seen["pause"] == 2.0
    assert seen["max_new"] == 3
    assert seen["push"] is False


def test_main_queue_end_to_end_with_mocked_fetch(monkeypatch, tmp_path, capsys):
    raw = tmp_path / "raw"
    raw.mkdir()
    queue = tmp_path / "fetch-queue.md"
    queue.write_text(f"# links\n{yt(ID_A)}\n", encoding="utf-8")
    monkeypatch.setattr(fetch, "fetch_youtube", lambda url, out, today, media=None: FakeFetcher(out)(url))
    monkeypatch.setattr(fetch_queue.time, "sleep", lambda s: None)
    code = fetch.main(["--queue", str(queue), "--out", str(raw), "--no-push"])
    assert code == 0
    assert queue.read_text(encoding="utf-8") == "# links\n"
    assert (tmp_path / "fetch-log.tsv").exists()


@pytest.mark.parametrize("videos, expected", [(0, "fetch: 0 videos"), (1, "fetch: 1 video"), (3, "fetch: 3 videos")])
def test_commit_message(videos, expected):
    assert fetch_queue.commit_message(videos) == expected


# --- the hosted fallback in the queue -----------------------------------

import fetch_hosted  # noqa: E402


class HostedFetcher(FakeFetcher):
    """Writes raw files the way the hosted fallback does (transcription: supadata-native)."""

    def __call__(self, url: str) -> Path:
        path = super().__call__(url)
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("video_id:", "transcription: supadata-native\nvideo_id:", 1), encoding="utf-8")
        return path


def test_a_hosted_fetch_is_logged_as_hosted(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n")
    env.run(HostedFetcher(env.raw))
    assert "transcription: supadata-native" in (env.raw / f"2026-01-01-{ID_A.lower()}.md").read_text()
    assert [(r[2], r[5]) for r in env.log_rows()] == [("fetched", "hosted")]


def test_a_local_fetch_keeps_an_empty_note(tmp_path):
    env = Env(tmp_path, f"{yt(ID_A)}\n")
    env.run(FakeFetcher(env.raw))
    assert env.log_rows()[0][5] == ""


def test_a_hosted_stop_stops_the_run_like_a_block(tmp_path, capsys):
    env = Env(tmp_path, f"{yt(ID_A)}\n{yt(ID_B)}\n")
    fetcher = FakeFetcher(env.raw, fail={ID_A: fetch_hosted.HostedStop("supadata: unauthorized: m")})
    result = env.run(fetcher)
    assert fetcher.calls == [yt(ID_A)]
    assert (result.exit_code, result.blocked) == (3, True)
    assert env.log_rows()[-1][5] == "supadata: unauthorized: m"
    assert "stopped: the hosted transcript API refused" in capsys.readouterr().out


def test_the_quota_guard_stops_the_run_with_exit_4_and_logs_nothing(tmp_path, capsys):
    env = Env(tmp_path, f"{yt(ID_A)}\n{yt(ID_B)}\n")
    fetcher = FakeFetcher(env.raw, fail={ID_A: fetch_hosted.HostedQuota("monthly hosted quota reached")})
    result = env.run(fetcher)
    assert fetcher.calls == [yt(ID_A)]
    assert (result.exit_code, result.stopped, result.blocked, result.failed) == (4, True, False, 0)
    assert not env.log.exists()
    assert env.queue.read_text(encoding="utf-8") == f"{yt(ID_A)}\n{yt(ID_B)}\n"
    assert "stopped: monthly hosted quota reached" in capsys.readouterr().out
