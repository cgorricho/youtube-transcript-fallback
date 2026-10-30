"""The scheduled loop: pull every clone, run fetch-many where the queue holds a link, back off after a block."""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

import schedule_fetch as sf

FETCH = [sys.executable, str(sf.HERE / "fetch.py"), "--queue", "fetch-queue.md", "--max", "8"]
PULL = ["git", "pull", "--ff-only", "-q"]


def clone(root: Path, name: str, queue: str | None = "- https://youtu.be/dQw4w9WgXcQ\n") -> Path:
    path = root / name
    (path / ".git").mkdir(parents=True)
    if queue is not None:
        (path / "fetch-queue.md").write_text(queue)
    return path


class Runner:
    """A fake runner: records (clone name, 'pull'|'fetch', argv) and answers from `codes` keyed by (clone name, step)."""

    def __init__(self, codes=None, raises=(), outputs=None):
        self.calls, self.codes, self.raises, self.outputs = [], codes or {}, set(raises), outputs or {}

    def __call__(self, argv, cwd, env):
        step = "pull" if argv[0] == "git" else "fetch"
        self.calls.append((Path(cwd).name, step, list(argv)))
        if (Path(cwd).name, step) in self.raises:
            raise OSError("python not found")
        return self.codes.get((Path(cwd).name, step), 0), self.outputs.get((Path(cwd).name, step), f"{step} output")


def offline(key):
    """The tests' default balance: no call ever leaves the machine."""
    raise OSError("offline in tests")


def run(root, runner, at=datetime(2026, 10, 10, 9, 30), **kw):
    log = root / "fetch-schedule.log"
    kw.setdefault("stretch", lambda: 1.0)
    kw.setdefault("balance", offline)
    code = sf.run(root, log_path=log, runner=runner, now=lambda: at, **kw)
    return code, log.read_text().splitlines()


def test_clones_are_the_git_folders_under_the_root(tmp_path):
    a, b = clone(tmp_path, "talks"), clone(tmp_path, "lectures")
    (tmp_path / ".venv").mkdir()
    (tmp_path / "notes").mkdir()
    other = clone(tmp_path / "elsewhere", "other")
    assert sf.clones(tmp_path) == [b, a]
    assert sf.clones(tmp_path, extra=[other, a]) == [b, a, other]


def test_each_clone_is_pulled_then_fetched_with_this_python(tmp_path):
    clone(tmp_path, "lectures")
    clone(tmp_path, "talks")
    runner = Runner()
    code, log = run(tmp_path, runner)
    assert runner.calls == [("lectures", "pull", PULL), ("lectures", "fetch", FETCH),
                            ("talks", "pull", PULL), ("talks", "fetch", FETCH)]
    assert code == 0
    assert log == ["2026-10-10T09:30:00 lectures: pulled, fetch-many exit 0",
                   "2026-10-10T09:30:00 talks: pulled, fetch-many exit 0"]


def test_a_clone_without_a_queue_is_only_pulled(tmp_path):
    clone(tmp_path, "a-no-queue", queue=None)
    clone(tmp_path, "b-ok")
    runner = Runner()
    code, log = run(tmp_path, runner)
    assert [c[:2] for c in runner.calls] == [("a-no-queue", "pull"), ("b-ok", "pull"), ("b-ok", "fetch")]
    assert log[0] == "2026-10-10T09:30:00 a-no-queue: pulled, no fetch-queue.md"
    assert code == 0


def test_a_queue_that_arrives_with_the_pull_is_fetched(tmp_path):
    # another machine pushed the clone's first fetch-queue.md: there is none here until the pull
    repo = clone(tmp_path, "talks", queue=None)

    class Pulls(Runner):
        def __call__(self, argv, cwd, env):
            if argv[0] == "git":
                (repo / "fetch-queue.md").write_text("- https://youtu.be/dQw4w9WgXcQ\n")
            return super().__call__(argv, cwd, env)

    runner = Pulls()
    code, log = run(tmp_path, runner)
    assert [c[:2] for c in runner.calls] == [("talks", "pull"), ("talks", "fetch")]
    assert log == ["2026-10-10T09:30:00 talks: pulled, fetch-many exit 0"] and code == 0


def test_an_empty_queue_after_the_pull_is_not_fetched(tmp_path):
    clone(tmp_path, "talks", queue="# Fetch queue\n\n## Added 2026-10-09\nnot a link\n")
    runner = Runner()
    code, log = run(tmp_path, runner)
    assert [c[:2] for c in runner.calls] == [("talks", "pull")]
    assert log == ["2026-10-10T09:30:00 talks: pulled, queue empty"] and code == 0


def test_a_failing_clone_does_not_stop_the_others(tmp_path):
    for name in ("a", "b", "c", "d"):
        clone(tmp_path, name)
    runner = Runner(codes={("a", "pull"): 1, ("b", "fetch"): 2}, raises={("c", "fetch")})
    code, log = run(tmp_path, runner)
    assert [c[:2] for c in runner.calls] == [("a", "pull"), ("b", "pull"), ("b", "fetch"), ("c", "pull"),
                                             ("c", "fetch"), ("d", "pull"), ("d", "fetch")]
    assert code == 1
    text = "\n".join(log)
    assert "a: git pull failed (exit 1): pull output" in text
    assert "b: pulled, fetch-many exit 2: fetch output" in text
    assert "c: pulled, fetch-many could not run: python not found" in text
    assert log[-1] == "2026-10-10T09:30:00 d: pulled, fetch-many exit 0"


def test_the_real_runner_passes_the_env_and_captures_output(tmp_path):
    code, out = sf.subprocess_runner([sys.executable, "-c", "import os; print(os.environ['SOME_VAR'])"],
                                     tmp_path, {"SOME_VAR": "x1"})
    assert (code, out.strip()) == (0, "x1")


def test_main_parses_its_arguments(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(sf, "run", lambda root, **kw: seen.update(root=root, **kw) or 0)
    assert sf.main(["--root", str(tmp_path), "--log", str(tmp_path / "x.log"), "--repo", str(tmp_path / "r1"),
                    "--repo", str(tmp_path / "r2"), "--per-run", "5"]) == 0
    assert seen["per_run"] == 5
    assert seen["root"] == tmp_path and seen["log_path"] == tmp_path / "x.log"
    assert seen["extra"] == [tmp_path / "r1", tmp_path / "r2"]
    sf.main(["--root", str(tmp_path)])
    assert seen["log_path"] == tmp_path / "fetch-schedule.log" and seen["extra"] == []


# ---- a cap per run and a backoff after a YouTube block, so the IP is not banned ----

def summary(n):
    return f"fetched: x\nfetched {n}, already processed 0, failed 0, left in queue 2"


def test_fetched_count_reads_the_summary_line():
    assert sf.fetched_count(summary(5)) == 5
    assert sf.fetched_count("nothing here") == 0


def test_the_run_limit_is_shared_across_clones(tmp_path):
    for name in ("a", "b", "c"):
        clone(tmp_path, name)
    runner = Runner(outputs={("a", "fetch"): summary(5), ("b", "fetch"): summary(3)})
    code, log = run(tmp_path, runner)
    assert [c[2][-1] for c in runner.calls if c[1] == "fetch"] == ["8", "3"]
    assert log[-1].endswith("c: pulled, not fetched: this run's link limit is reached") and code == 0


def test_a_block_stops_the_run_and_backs_off_30_minutes(tmp_path):
    for name in ("a", "b"):
        clone(tmp_path, name)
    runner = Runner(codes={("a", "fetch"): 3}, outputs={("a", "fetch"): "IpBlocked " + summary(2)})
    code, log = run(tmp_path, runner, stretch=lambda: 1.1)
    assert [c[:2] for c in runner.calls] == [("a", "pull"), ("a", "fetch")]
    assert code == 1
    assert log[-1] == ("2026-10-10T09:30:00 YouTube blocked this connection (block 1 in a row): "
                       "no fetch until 2026-10-10T10:03; the remaining clones wait")
    state = json.loads((tmp_path / "fetch-backoff.json").read_text())
    assert state == {"level": 1, "until": "2026-10-10T10:03:00"}


def test_runs_inside_the_backoff_do_nothing(tmp_path):
    clone(tmp_path, "a")
    (tmp_path / "fetch-backoff.json").write_text('{"level": 2, "until": "2026-10-10T10:30:00"}')
    runner = Runner()
    code, log = run(tmp_path, runner)
    assert runner.calls == [] and code == 0
    assert log == ["2026-10-10T09:30:00 backing off after 2 YouTube block(s) in a row: no fetch until 2026-10-10T10:30"]


def test_each_block_in_a_row_doubles_the_wait_up_to_4_hours(tmp_path):
    clone(tmp_path, "a")
    (tmp_path / "fetch-backoff.json").write_text('{"level": 1, "until": "2026-10-10T09:00:00"}')
    runner = Runner(codes={("a", "fetch"): 3})
    run(tmp_path, runner)
    assert json.loads((tmp_path / "fetch-backoff.json").read_text())["until"] == "2026-10-10T10:30:00"
    assert [sf.backoff_minutes(n, 1.0) for n in (1, 2, 3, 4, 5, 9)] == [30, 60, 120, 240, 240, 240]


def test_a_clean_fetch_clears_the_backoff(tmp_path):
    clone(tmp_path, "a")
    (tmp_path / "fetch-backoff.json").write_text('{"level": 3, "until": "2026-10-10T09:00:00"}')
    code, log = run(tmp_path, Runner(outputs={("a", "fetch"): summary(4)}))
    assert not (tmp_path / "fetch-backoff.json").exists()
    assert log[-1] == "2026-10-10T09:30:00 fetched without a block: backoff cleared"


def test_a_run_that_fetches_nothing_keeps_the_level(tmp_path):
    clone(tmp_path, "a", queue="")
    (tmp_path / "fetch-backoff.json").write_text('{"level": 3, "until": "2026-10-10T09:00:00"}')
    run(tmp_path, Runner())
    assert json.loads((tmp_path / "fetch-backoff.json").read_text())["level"] == 3


def test_a_broken_backoff_file_is_ignored(tmp_path):
    clone(tmp_path, "a")
    (tmp_path / "fetch-backoff.json").write_text("not json")
    runner = Runner()
    run(tmp_path, runner)
    assert [c[1] for c in runner.calls] == ["pull", "fetch"]


# ---- the hosted key and the quota guard ----

HEADER = "date\tvideo_id\tresult\traw_file\turl\tnote\n"


def hosted_log(path: Path, rows) -> None:
    with open(path / "fetch-log.tsv", "a", encoding="utf-8") as f:
        if f.tell() == 0:
            f.write(HEADER)
        for day, result, note in rows:
            f.write(f"{day}\tid\t{result}\tx.md\thttps://youtu.be/x\t{note}\n")


def test_hosted_used_counts_this_months_hosted_rows(tmp_path):
    a, b = clone(tmp_path, "a"), clone(tmp_path, "b")
    hosted_log(a, [("2026-10-01", "fetched", "hosted"), ("2026-10-02", "failed", "hosted: no captions on this video"),
                   ("2026-10-03", "fetched", ""), ("2026-09-30", "fetched", "hosted"),
                   ("2026-10-04", "failed", "supadata: HTTP 401"), ("2026-10-05", "duplicate", "")])
    hosted_log(b, [("2026-10-19", "fetched", "hosted")])
    assert sf.hosted_used([a / "fetch-log.tsv", b / "fetch-log.tsv", tmp_path / "none.tsv"],
                          datetime(2026, 10, 20).date()) == 3


class EnvRunner(Runner):
    """Records the hosted variables each fetch sees; a fetch in `hosts` logs one hosted row, as the real one would."""

    def __init__(self, hosts=(), **kw):
        super().__init__(**kw)
        self.hosts, self.seen = set(hosts), []

    def __call__(self, argv, cwd, env):
        if argv[0] != "git":
            self.seen.append((Path(cwd).name, env.get("SUPADATA_API_KEY"), env.get("SUPADATA_CREDITS_LEFT")))
            if Path(cwd).name in self.hosts:
                hosted_log(Path(cwd), [("2026-10-10", "fetched", "hosted")])
        return super().__call__(argv, cwd, env)


class Balance:
    """Stands in for the /v1/me read: records each call, answers (left, max) or raises."""

    def __init__(self, answer=(50, 600)):
        self.answer, self.keys = answer, []

    def __call__(self, key):
        self.keys.append(key)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_with_a_key_the_balance_is_read_once_and_each_fetch_gets_it_less_the_runs_hosted_rows(tmp_path):
    a, b = clone(tmp_path, "a"), clone(tmp_path, "b")
    hosted_log(b, [("2026-10-01", "fetched", "hosted")] * 3)
    runner, balance = EnvRunner(hosts={"a"}), Balance((50, 600))
    code, log = run(tmp_path, runner, hosted_key="sekret", hosted_quota=10, balance=balance)
    assert balance.keys == ["sekret"]
    assert runner.seen == [("a", "sekret", "50"), ("b", "sekret", "49")]
    assert log[0] == "2026-10-10T09:30:00 hosted credits: 50 left of 600 (supadata)"
    assert code == 0
    assert "sekret" not in (tmp_path / "fetch-schedule.log").read_text()


def test_the_balance_is_read_at_the_first_clone_whose_queue_holds_a_youtube_link(tmp_path):
    clone(tmp_path, "a-other", queue="- https://example.com/talk\n")
    clone(tmp_path, "b-yt")
    clone(tmp_path, "c-yt")
    runner, balance = EnvRunner(), Balance((5, 100))
    _, log = run(tmp_path, runner, hosted_key="k", hosted_quota=10, balance=balance)
    assert balance.keys == ["k"]
    assert runner.seen == [("a-other", "k", "10"), ("b-yt", "k", "5"), ("c-yt", "k", "5")]
    assert [line.split(" ", 1)[1] for line in log] == [
        "a-other: pulled, fetch-many exit 0", "hosted credits: 5 left of 100 (supadata)",
        "b-yt: pulled, fetch-many exit 0", "c-yt: pulled, fetch-many exit 0"]


def test_no_youtube_link_in_any_queue_means_no_balance_call(tmp_path):
    clone(tmp_path, "a", queue="- https://example.com/post\n")
    clone(tmp_path, "b", queue="# nothing yet\n")
    clone(tmp_path, "c", queue=None)
    balance = Balance()
    _, log = run(tmp_path, EnvRunner(), hosted_key="k", balance=balance)
    assert balance.keys == []
    assert not any("hosted credits" in line for line in log)


def test_without_a_key_nothing_hosted_is_passed_logged_or_asked(tmp_path):
    clone(tmp_path, "a")
    runner, balance = EnvRunner(), Balance()
    code, log = run(tmp_path, runner, balance=balance)
    assert runner.seen == [("a", None, None)]
    assert balance.keys == []
    assert not any("hosted" in line for line in log)


def test_when_me_fails_the_guard_falls_back_to_the_local_count_and_says_why(tmp_path):
    a, b = clone(tmp_path, "a"), clone(tmp_path, "b")
    hosted_log(b, [("2026-10-01", "fetched", "hosted")] * 3)
    runner, balance = EnvRunner(hosts={"a"}), Balance(OSError("HTTP 500"))
    _, log = run(tmp_path, runner, hosted_key="sekret", hosted_quota=10, balance=balance)
    assert balance.keys == ["sekret"]
    assert runner.seen == [("a", "sekret", "7"), ("b", "sekret", "6")]
    assert log[0] == "2026-10-10T09:30:00 hosted credits: 7 left of 10 (local count; supadata /me: HTTP 500)"


def test_the_fallback_line_never_shows_the_key(tmp_path):
    clone(tmp_path, "a")
    _, log = run(tmp_path, EnvRunner(), hosted_key="sekret", balance=Balance(OSError("bad key sekret\nline")))
    assert log[0] == "2026-10-10T09:30:00 hosted credits: 100 left of 100 (local count; supadata /me: bad key *** line)"


def test_the_credits_left_never_go_below_zero(tmp_path):
    a = clone(tmp_path, "a")
    hosted_log(a, [("2026-10-01", "fetched", "hosted")] * 12)
    runner = EnvRunner()
    run(tmp_path, runner, hosted_key="k", hosted_quota=10)
    assert runner.seen == [("a", "k", "0")]
    b = clone(tmp_path, "b")
    runner = EnvRunner(hosts={"a"})
    run(tmp_path, runner, hosted_key="k", balance=Balance((0, 100)))
    assert runner.seen == [("a", "k", "0"), ("b", "k", "0")]


def test_youtube_links_are_found_in_any_queue_line_form(tmp_path):
    queue = tmp_path / "q.md"
    for text, found in [("- https://youtu.be/dQw4w9WgXcQ\n", True), ("<https://www.youtube.com/watch?v=x>\n", True),
                        ("* https://m.youtube.com/watch?v=x\n", True), ("- https://example.com/youtube.com\n", False),
                        ("see https://youtu.be/x\n", False), ("", False)]:
        queue.write_text(text)
        assert sf.has_youtube_links(queue) is found, text


def test_the_youtube_hosts_are_fetchs():
    import fetch

    assert sf.YOUTUBE_HOSTS == fetch.YOUTUBE_HOSTS


def test_the_default_balance_reads_me_through_the_hosted_client(monkeypatch):
    import fetch_hosted

    seen = []

    class Client:
        def _request(self, method, path):
            seen.append((method, path))
            return {"max_credits": 100, "used_credits": 30}

    monkeypatch.setattr(fetch_hosted, "make_client", lambda key: seen.append(key) or Client())
    assert sf.supadata_balance("k") == (70, 100)
    assert seen == ["k", ("GET", "/me")]


def test_a_quota_stop_is_not_a_failure_and_the_other_clones_still_run(tmp_path):
    clone(tmp_path, "a")
    clone(tmp_path, "b")
    runner = Runner(codes={("a", "fetch"): 4})
    code, log = run(tmp_path, runner, hosted_key="k")
    assert code == 0
    assert log[1] == "2026-10-10T09:30:00 a: pulled, hosted quota reached; YouTube links wait"
    assert [c[0] for c in runner.calls if c[1] == "fetch"] == ["a", "b"]
    assert not (tmp_path / sf.BACKOFF_FILE).exists()


def test_main_reads_the_key_from_the_env_file_and_the_quota(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(sf, "run", lambda root, **kw: seen.update(**kw) or 0)
    env = tmp_path / ".env"
    env.write_text("SUPADATA_API_KEY=k1\n")
    monkeypatch.delenv("SUPADATA_API_KEY", raising=False)
    sf.main(["--root", str(tmp_path), "--env-file", str(env), "--hosted-quota", "50"])
    assert (seen["hosted_key"], seen["hosted_quota"]) == ("k1", 50)
    sf.main(["--root", str(tmp_path), "--env-file", str(tmp_path / "missing")])
    assert (seen["hosted_key"], seen["hosted_quota"]) == (None, 100)


def test_the_default_env_file_is_beside_the_script():
    assert sf.ENV_FILE == Path(sf.__file__).resolve().parent / ".env"
