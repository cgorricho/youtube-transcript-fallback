"""The hosted adapter driven through the real Supadata SDK client, over a fake transport.

test_fetch_hosted.py fakes the client; these tests keep the SDK itself (request building,
camelCase mapping, 206 and error-body handling) in the path and replace only its
`requests.Session.request`, so no call leaves the machine and an SDK upgrade that changes
any of it fails here.
"""
import importlib.metadata
import json

import pytest
import requests

import fetch_hosted as fh
from fetch import FetchError, Fragment

ID = "qShed6dyrQY"
KEY = "test-key-not-real"
BASE = "https://api.supadata.ai/v1"
PINNED = "1.6.0"


def response(status, body=None, url=f"{BASE}/transcript"):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = b"" if body is None else json.dumps(body).encode()
    r.headers["Content-Type"] = "application/json"
    return r


class Transport:
    """Stands in for the SDK session's request(): answers in order, records every call."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.answers.pop(0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t


def sdk_client(transport):
    client = fh.make_client(KEY)
    client.session.request = transport
    return client


def get(transport):
    clock = Clock()
    return fh.hosted_transcript(ID, sdk_client(transport), sleep=clock.sleep, clock=clock)


CHUNKS = [{"text": "hello", "offset": 0, "duration": 1500, "lang": "en"},
          {"text": "second", "offset": 61500, "duration": 2000, "lang": "en"}]


def test_the_pinned_sdk_is_installed():
    assert importlib.metadata.version("supadata") == PINNED


def test_the_sdk_sends_one_native_request_with_the_key_header_and_its_own_user_agent():
    transport = Transport(response(200, {"content": CHUNKS, "lang": "en", "availableLangs": ["en"]}))
    got = get(transport)
    assert got.lang == "en"
    assert got.fragments == [Fragment("hello", 0.0), Fragment("second", 61.5)]
    assert got.duration == 64
    [call] = transport.calls
    assert (call["method"], call["url"]) == ("GET", f"{BASE}/transcript")
    assert call["params"] == {"url": f"https://www.youtube.com/watch?v={ID}", "mode": "native"}
    client = sdk_client(transport)
    assert client.session.headers["x-api-key"] == KEY
    assert client.session.headers["User-Agent"] == f"Supadata-Python-SDK/{PINNED}"


def test_a_job_id_answer_is_polled_on_the_transcript_job_endpoint():
    transport = Transport(
        response(202, {"jobId": "j1"}),
        response(200, {"status": "active"}, url=f"{BASE}/transcript/j1"),
        response(200, {"status": "completed", "content": CHUNKS, "lang": "en"}, url=f"{BASE}/transcript/j1"),
    )
    got = get(transport)
    assert [c["url"] for c in transport.calls] == [f"{BASE}/transcript"] + [f"{BASE}/transcript/j1"] * 2
    assert got.fragments[1] == Fragment("second", 61.5)


def test_206_is_no_captions_not_a_stop():
    with pytest.raises(FetchError, match="^hosted: no captions") as exc:
        get(Transport(response(206, {})))
    assert not isinstance(exc.value, fh.HostedStop)


@pytest.mark.parametrize("status,code,error,exit_code", [(401, "unauthorized", fh.HostedStop, 3),
                                                          (429, "limit-exceeded", fh.HostedQuota, 4)])
def test_a_documented_error_body_stops_the_run(status, code, error, exit_code):
    body = {"error": code, "message": "m", "details": "d", "documentationUrl": "https://docs.supadata.ai"}
    with pytest.raises(error) as exc:
        get(Transport(response(status, body)))
    assert exc.value.exit_code == exit_code
    assert KEY not in str(exc.value)


@pytest.mark.parametrize("status,body", [(429, {"message": "Too many requests"}), (403, None),
                                         (401, {"error": "unauthorized", "message": "m", "extra": 1})])
def test_an_error_body_the_sdk_cannot_parse_still_stops_on_the_status(status, body):
    with pytest.raises(fh.HostedStop, match=f"HTTP {status}"):
        get(Transport(response(status, body)))


def test_a_server_error_is_a_plain_failure():
    with pytest.raises(FetchError, match="HTTP 500") as exc:
        get(Transport(response(500, {"message": "boom"})))
    assert not isinstance(exc.value, fh.HostedStop)


def test_the_balance_is_read_from_me_through_the_sdk():
    me = {"organizationId": "org-1", "plan": "Free (100/mo)", "maxCredits": 600, "usedCredits": 100}
    transport = Transport(response(200, me, url=f"{BASE}/me"))
    assert fh.balance(sdk_client(transport)) == (500, 600)
    [call] = transport.calls
    assert (call["method"], call["url"]) == ("GET", f"{BASE}/me")


@pytest.mark.parametrize("status,body", [(401, {"error": "unauthorized", "message": "m", "details": "d"}),
                                         (500, {"message": "boom"}), (200, {"plan": "Free"})])
def test_a_balance_the_sdk_cannot_read_is_a_fetch_error(status, body):
    with pytest.raises(FetchError) as exc:
        fh.balance(sdk_client(Transport(response(status, body, url=f"{BASE}/me"))))
    assert not isinstance(exc.value, (fh.HostedStop, fh.HostedQuota))
    assert KEY not in str(exc.value)
