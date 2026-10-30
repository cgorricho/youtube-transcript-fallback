"""The hosted transcript fallback (Supadata), with a fake client: no network."""
import pytest
import requests
from supadata import SupadataError
from supadata.types import BatchJob, Transcript

import fetch_hosted as fh
from fetch import FetchError, Fragment

ID = "qShed6dyrQY"


class FakeClient:
    """Answers transcript() from `answers` in order; polls (_request) from `polls` in order."""

    def __init__(self, answers, polls=()):
        self.answers, self.polls = list(answers), list(polls)
        self.calls, self.poll_paths = [], []

    def transcript(self, url, lang=None, text=False, chunk_size=None, mode="auto"):
        self.calls.append({"url": url, "lang": lang, "text": text, "mode": mode})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _request(self, method, path, **kwargs):
        self.poll_paths.append((method, path))
        answer = self.polls.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def transcript(lang="en", available=("en",), chunks=None):
    chunks = chunks if chunks is not None else [
        {"text": "hello  there", "offset": 0, "duration": 1500, "lang": lang},
        {"text": "second", "offset": 61500, "duration": 2000, "lang": lang},
    ]
    return Transcript(content=chunks, lang=lang, available_langs=list(available))


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


def get(client, **kw):
    clock = kw.pop("clock", Clock())
    return fh.hosted_transcript(ID, client, sleep=clock.sleep, clock=clock, **kw)


def test_segments_map_to_fragments_in_seconds_and_only_native_captions_are_asked():
    client = FakeClient([transcript()])
    got = get(client)
    assert got.lang == "en"
    assert got.fragments == [Fragment("hello  there", 0.0), Fragment("second", 61.5)]
    assert got.duration == 64
    assert client.calls == [{"url": f"https://www.youtube.com/watch?v={ID}", "lang": None, "text": False,
                             "mode": "native"}]


def test_chunk_objects_from_the_sdk_map_the_same_way():
    from supadata.types import TranscriptChunk

    t = Transcript(content=[TranscriptChunk(text="a", offset=2000, duration=1000, lang="es")], lang="es",
                   available_langs=["es"])
    got = get(FakeClient([t]))
    assert (got.lang, got.fragments, got.duration) == ("es", [Fragment("a", 2.0)], 3)


def test_a_language_other_than_en_or_es_asks_again_for_an_accepted_one():
    client = FakeClient([transcript("fr", ("fr", "es")), transcript("es", ("fr", "es"))])
    assert get(client).lang == "es"
    assert [c["lang"] for c in client.calls] == [None, "es"]


def test_no_accepted_language_fails_without_a_second_call():
    client = FakeClient([transcript("fr", ("fr", "de"))])
    with pytest.raises(FetchError, match="hosted: no en or es captions; available: fr, de"):
        get(client)
    assert len(client.calls) == 1


def test_an_async_job_is_polled_every_2_seconds_until_completed():
    done = {"status": "completed", "content": [{"text": "x", "offset": 1000, "duration": 500}], "lang": "en"}
    client = FakeClient([BatchJob(job_id="j1")], polls=[{"status": "queued"}, {"status": "active"}, done])
    clock = Clock()
    got = get(client, clock=clock)
    assert got.fragments == [Fragment("x", 1.0)]
    assert client.poll_paths == [("GET", "/transcript/j1")] * 3
    assert clock.sleeps == [2, 2]


def test_a_failed_job_is_a_fetch_error():
    client = FakeClient([BatchJob(job_id="j1")], polls=[{"status": "failed", "error": "boom"}])
    with pytest.raises(FetchError, match="hosted: transcript job failed: boom") as exc:
        get(client)
    assert not isinstance(exc.value, fh.HostedStop)


def test_a_job_still_running_after_10_minutes_times_out():
    client = FakeClient([BatchJob(job_id="j1")], polls=[{"status": "active"}] * 400)
    with pytest.raises(FetchError, match="hosted: transcript job j1 still running after 600 s"):
        get(client)


def sdk_error(code):
    return SupadataError(error=code, message="m", details="d")


def test_no_transcript_is_a_plain_failure_marked_hosted():
    with pytest.raises(FetchError, match="^hosted: no captions") as exc:
        get(FakeClient([sdk_error("transcript-unavailable")]))
    assert not isinstance(exc.value, fh.HostedStop)


@pytest.mark.parametrize("code", ["unauthorized", "forbidden", "upgrade-required"])
def test_key_errors_stop_the_run(code):
    with pytest.raises(fh.HostedStop) as exc:
        get(FakeClient([sdk_error(code)]))
    assert exc.value.exit_code == 3
    assert str(exc.value).startswith("supadata:")


def test_limit_exceeded_is_the_quota_not_a_block():
    # the API is the last guard (bonus credits /me may not show); links wait, no backoff
    with pytest.raises(fh.HostedQuota) as exc:
        get(FakeClient([sdk_error("limit-exceeded")]))
    assert exc.value.exit_code == 4
    assert str(exc.value).startswith("supadata: limit-exceeded")


@pytest.mark.parametrize("status,stops", [(401, True), (403, True), (429, True), (500, False)])
def test_http_errors_without_a_json_body_map_by_status(status, stops):
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError("bad", response=response)
    with pytest.raises(FetchError) as exc:
        get(FakeClient([error]))
    assert isinstance(exc.value, fh.HostedStop) is stops
    assert f"HTTP {status}" in str(exc.value)


def test_other_sdk_errors_and_network_errors_are_failures():
    with pytest.raises(FetchError, match="hosted: m"):
        get(FakeClient([sdk_error("internal-error")]))
    with pytest.raises(FetchError, match="hosted: ConnectionError"):
        get(FakeClient([requests.ConnectionError("down")]))


def test_read_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# c\nOTHER=1\nexport SUPADATA_API_KEY = 'abc'\n\nBAD LINE\n")
    assert fh.read_env_file(env) == {"OTHER": "1", "SUPADATA_API_KEY": "abc"}
    assert fh.read_env_file(tmp_path / "missing") == {}


def test_settings_come_from_the_environment_first_then_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SUPADATA_API_KEY=fromfile\n")
    assert fh.settings_from_env({}, [tmp_path / "none"]) is None
    assert fh.settings_from_env({}, [tmp_path / "none", env]).key == "fromfile"
    s = fh.settings_from_env({"SUPADATA_API_KEY": "fromenv", "SUPADATA_CREDITS_LEFT": "7"}, [env])
    assert (s.key, s.credits_left) == ("fromenv", 7)
    assert fh.settings_from_env({"SUPADATA_API_KEY": "k", "SUPADATA_CREDITS_LEFT": "x"}, [env]).credits_left is None


def test_the_key_never_shows_in_the_settings_repr():
    assert "secret123" not in repr(fh.HostedSettings("secret123"))


def test_oembed_parses_title_and_channel_and_tolerates_failure():
    calls = []

    def http(url):
        calls.append(url)
        return {"title": "Sample", "author_name": "Sample Channel"}

    assert fh.oembed(ID, http) == {"title": "Sample", "author_name": "Sample Channel"}
    assert calls == [f"https://www.youtube.com/oembed?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D{ID}&format=json"]

    def broken(url):
        raise OSError("down")

    assert fh.oembed(ID, broken) == {}


def test_the_env_file_is_beside_the_module():
    from pathlib import Path

    assert fh.ENV_FILES == (Path(fh.__file__).resolve().parent / ".env",)


# ---- the balance from /v1/me ----

def test_balance_is_credits_left_and_the_maximum_from_me():
    client = FakeClient([], polls=[{"organization_id": "org", "plan": "Free", "max_credits": 600, "used_credits": 100}])
    assert fh.balance(client) == (500, 600)
    assert client.poll_paths == [("GET", "/me")]


def test_balance_never_goes_below_zero():
    assert fh.balance(FakeClient([], polls=[{"max_credits": 100, "used_credits": 104}])) == (0, 100)


@pytest.mark.parametrize("answer", [{"max_credits": 100}, {"used_credits": 1}, {"max_credits": "x", "used_credits": 1},
                                    [], requests.ConnectionError("down"), sdk_error("unauthorized")])
def test_any_balance_error_is_a_fetch_error(answer):
    with pytest.raises(FetchError) as exc:
        fh.balance(FakeClient([], polls=[answer]))
    assert not isinstance(exc.value, (fh.HostedStop, fh.HostedQuota))
    assert "org" not in str(exc.value)
