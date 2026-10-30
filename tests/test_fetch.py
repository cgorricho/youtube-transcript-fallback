from datetime import date
from pathlib import Path

import fetch
import pytest
from fetch import FetchError, video_id_from_url, Track, select_track, base_language, Fragment, format_timestamp, render_transcript, slugify, raw_filename, Metadata, yaml_string, render_document, write_raw, canonical_url, metadata_from_info


def test_module_imports():
    assert fetch.__doc__


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=zjkBMFhNj_g",
    "https://youtube.com/watch?v=zjkBMFhNj_g&t=120s",
    "https://m.youtube.com/watch?v=zjkBMFhNj_g",
    "https://youtu.be/zjkBMFhNj_g",
    "https://youtu.be/zjkBMFhNj_g?si=abc",
    "https://www.youtube.com/shorts/zjkBMFhNj_g",
    "https://www.youtube.com/live/zjkBMFhNj_g",
    "https://www.youtube.com/embed/zjkBMFhNj_g",
])
def test_video_id_from_supported_url_forms(url):
    assert video_id_from_url(url) == "zjkBMFhNj_g"


def test_unsupported_host_exits_2():
    with pytest.raises(FetchError) as exc:
        video_id_from_url("https://vimeo.com/12345")
    assert exc.value.exit_code == 2
    assert "unsupported host" in str(exc.value)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/",
    "https://www.youtube.com/watch",
    "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/channel/UCabc",
])
def test_unrecognised_youtube_url_exits_2(url):
    with pytest.raises(FetchError) as exc:
        video_id_from_url(url)
    assert exc.value.exit_code == 2
    assert "not a recognised YouTube URL" in str(exc.value)


def test_base_language_strips_region():
    assert base_language("en-US") == "en"
    assert base_language("es-419") == "es"
    assert base_language("EN") == "en"


def test_manual_in_spoken_language_wins():
    tracks = [Track("en", False), Track("es", False), Track("es", True)]
    assert select_track(tracks, spoken="es") == Track("es", False)


def test_manual_beats_auto_even_in_other_accepted_language():
    tracks = [Track("es", False), Track("en", True)]
    assert select_track(tracks, spoken="en") == Track("es", False)


def test_english_wins_when_spoken_unknown_and_both_manual():
    tracks = [Track("es", False), Track("en", False)]
    assert select_track(tracks, spoken=None) == Track("en", False)


def test_auto_used_when_no_manual():
    tracks = [Track("fr", False), Track("en-US", True)]
    assert select_track(tracks, spoken="en") == Track("en-US", True)


def test_no_usable_track_exits_1_listing_available():
    tracks = [Track("fr", False), Track("de", True)]
    with pytest.raises(FetchError) as exc:
        select_track(tracks, spoken="fr")
    assert exc.value.exit_code == 1
    assert "no usable captions" in str(exc.value)
    assert "fr (manual)" in str(exc.value)
    assert "de (auto)" in str(exc.value)


def test_no_tracks_at_all():
    with pytest.raises(FetchError) as exc:
        select_track([], spoken=None)
    assert "available: none" in str(exc.value)


def test_auto_prefers_spoken_language_when_both_auto_exist():
    tracks = [Track("en", True), Track("es", True)]
    assert select_track(tracks, spoken="es") == Track("es", True)
    assert select_track(tracks, spoken=None) == Track("en", True)


@pytest.mark.parametrize("seconds,expected", [
    (0, "0:00"),
    (7.9, "0:07"),
    (65, "1:05"),
    (599, "9:59"),
    (600, "10:00"),
    (3599, "59:59"),
    (3600, "1:00:00"),
    (3661.4, "1:01:01"),
    (36000, "10:00:00"),
])
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_one_paragraph_per_minute_with_marker_of_first_fragment():
    fragments = [
        Fragment("hello", 0.0),
        Fragment("world", 30.5),
        Fragment("second", 62.0),
        Fragment("minute", 90.0),
        Fragment("third", 125.0),
    ]
    assert render_transcript(fragments) == (
        "[0:00] hello world\n\n"
        "[1:02] second minute\n\n"
        "[2:05] third"
    )


def test_whitespace_and_newlines_inside_fragments_are_normalised():
    fragments = [Fragment("line one\nline  two ", 0.0), Fragment("  three", 1.0)]
    assert render_transcript(fragments) == "[0:00] line one line two three"


def test_empty_fragments_are_skipped():
    fragments = [Fragment("", 0.0), Fragment("   ", 61.0), Fragment("x", 122.0)]
    assert render_transcript(fragments) == "[2:02] x"


def test_no_fragments_gives_empty_string():
    assert render_transcript([]) == ""


def test_hour_marker():
    fragments = [Fragment("late", 3725.0)]
    assert render_transcript(fragments) == "[1:02:05] late"


@pytest.mark.parametrize("title,expected", [
    ("Intro to Large Language Models", "intro-to-large-language-models"),
    ("  Spaces   and -- dashes  ", "spaces-and-dashes"),
    ("Qué es un LLM? ¡Explicación! (2026)", "que-es-un-llm-explicacion-2026"),
    ("C++ & Rust: a comparison", "c-rust-a-comparison"),
    ("日本語のみ", "untitled"),
    ("", "untitled"),
])
def test_slugify(title, expected):
    assert slugify(title) == expected


def test_slugify_truncates_at_hyphen_boundary():
    title = "one two three four five six seven eight nine ten eleven twelve thirteen"
    slug = slugify(title)
    assert len(slug) <= 60
    assert not slug.endswith("-")
    assert slug == "one-two-three-four-five-six-seven-eight-nine-ten-eleven"


def test_slugify_truncates_single_long_word():
    assert slugify("a" * 70) == "a" * 60


def test_raw_filename():
    assert raw_filename(date(2023, 11, 22), "Intro to LLMs") == "2023-11-22-intro-to-llms.md"


def _meta(**overrides):
    base = dict(
        title='Intro to "Large" Language Models',
        channel="Andrej Karpathy",
        upload_date=date(2023, 11, 22),
        duration=3600 + 65,
        description="Slides: https://example.com\n\nSecond line",
        url="https://www.youtube.com/watch?v=zjkBMFhNj_g",
        language="en",
        video_id="zjkBMFhNj_g",
    )
    base.update(overrides)
    return Metadata(**base)


def test_canonical_url():
    assert canonical_url("zjkBMFhNj_g") == "https://www.youtube.com/watch?v=zjkBMFhNj_g"


def _info(**overrides):
    base = dict(
        title='Intro to "Large" Language Models',
        channel="Andrej Karpathy",
        uploader="Andrej Karpathy (uploader)",
        upload_date="20231123",
        duration="3588",
        description="Slides: https://example.com",
        webpage_url="https://www.youtube.com/watch?v=zjkBMFhNj_g",
        language="en",
    )
    base.update(overrides)
    return base


def test_metadata_from_info_maps_every_field():
    info = _info()
    meta = metadata_from_info(info, "zjkBMFhNj_g", today=date(2026, 9, 16))
    assert meta == Metadata(
        title='Intro to "Large" Language Models',
        channel="Andrej Karpathy",
        upload_date=date(2023, 11, 23),
        duration=3588,
        description="Slides: https://example.com",
        url="https://www.youtube.com/watch?v=zjkBMFhNj_g",
        language="en",
        video_id="zjkBMFhNj_g",
    )


def test_metadata_from_info_duration_accepts_int():
    info = _info(duration=3588)
    meta = metadata_from_info(info, "zjkBMFhNj_g", today=date(2026, 9, 16))
    assert meta.duration == 3588


def test_metadata_from_info_missing_upload_date_uses_today():
    info = _info()
    del info["upload_date"]
    meta = metadata_from_info(info, "zjkBMFhNj_g", today=date(2026, 9, 16))
    assert meta.upload_date == date(2026, 9, 16)


def test_metadata_from_info_channel_falls_back_to_uploader_then_unknown():
    info = _info()
    del info["channel"]
    meta = metadata_from_info(info, "zjkBMFhNj_g", today=date(2026, 9, 16))
    assert meta.channel == "Andrej Karpathy (uploader)"

    del info["uploader"]
    meta = metadata_from_info(info, "zjkBMFhNj_g", today=date(2026, 9, 16))
    assert meta.channel == "unknown"


def test_metadata_from_info_url_falls_back_to_canonical():
    info = {"webpage_url": None}
    meta = metadata_from_info(info, "zjkBMFhNj_g", today=date(2026, 9, 16))
    assert meta.url == "https://www.youtube.com/watch?v=zjkBMFhNj_g"
    assert meta.title == "zjkBMFhNj_g"
    assert meta.channel == "unknown"
    assert meta.duration == 0
    assert meta.description == ""
    assert meta.language is None
    assert meta.upload_date == date(2026, 9, 16)


def test_yaml_string_quotes_and_escapes():
    assert yaml_string('a "b" c') == '"a \\"b\\" c"'
    assert yaml_string("café") == '"café"'


def test_render_document_frontmatter_and_body():
    doc = render_document(
        _meta(), Track("en-US", True),
        [Fragment("hello", 0.0), Fragment("again", 60.0)],
        created=date(2026, 9, 16),
    )
    assert doc.startswith("---\n")
    head, body = doc.split("\n---\n", 1)
    assert head.split("\n")[1:] == [
        'title: "Intro to \\"Large\\" Language Models"',
        'author: "Andrej Karpathy"',
        'source: "https://www.youtube.com/watch?v=zjkBMFhNj_g"',
        "published: 2023-11-22",
        "created: 2026-09-16",
        "type: youtube-transcript",
        'video_id: "zjkBMFhNj_g"',
        'duration: "1:01:05"',
        "captions: auto",
        "language: en",
        "tags:",
        '  - "clippings"',
    ]
    assert body == (
        '\n# Intro to "Large" Language Models\n'
        "\n## Description\n"
        "\nSlides: https://example.com\n\nSecond line\n"
        "\n## Transcript\n"
        "\n[0:00] hello\n\n[1:00] again\n"
    )


def test_render_document_manual_captions_and_empty_description():
    doc = render_document(_meta(description="   "), Track("es", False), [], created=date(2026, 9, 16))
    assert "captions: manual\n" in doc
    assert "language: es\n" in doc
    assert "## Description\n\n(none)\n" in doc


def test_write_raw_writes_and_returns_path(tmp_path):
    target = write_raw(tmp_path, "2023-11-22-x.md", "content\n")
    assert target == tmp_path / "2023-11-22-x.md"
    assert target.read_text(encoding="utf-8") == "content\n"


def test_write_raw_refuses_to_overwrite(tmp_path):
    (tmp_path / "2023-11-22-x.md").write_text("original")
    with pytest.raises(FetchError) as exc:
        write_raw(tmp_path, "2023-11-22-x.md", "new")
    assert exc.value.exit_code == 1
    assert "refusing to overwrite" in str(exc.value)
    assert (tmp_path / "2023-11-22-x.md").read_text() == "original"


def test_write_raw_requires_existing_directory(tmp_path):
    with pytest.raises(FetchError) as exc:
        write_raw(tmp_path / "missing", "x.md", "c")
    assert "output directory does not exist" in str(exc.value)


from fetch import dispatch, main


@pytest.mark.parametrize("url", ["ftp://example.com/page", "https://example.com/paper.pdf", "https://vimeo.com/1"])
def test_dispatch_rejects_anything_but_youtube_without_network(tmp_path, url):
    with pytest.raises(FetchError) as exc:
        dispatch(url, tmp_path, date(2026, 9, 16))
    assert exc.value.exit_code == 2


def test_main_reports_fetch_error_on_stderr(capsys):
    code = main(["ftp://example.com/page", "--out", "."])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("fetch: unsupported host")


# --- edge cases ------------------------------------------------------------------


def test_write_raw_refuses_when_file_appears_after_the_check(tmp_path, monkeypatch):
    """Exclusive create: a file that appears between any check and the write is not overwritten."""
    (tmp_path / "2023-11-22-x.md").write_text("original")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(FetchError) as exc:
        write_raw(tmp_path, "2023-11-22-x.md", "new")
    assert exc.value.exit_code == 1
    assert "refusing to overwrite" in str(exc.value)
    assert (tmp_path / "2023-11-22-x.md").read_text() == "original"


def test_main_turns_unexpected_exceptions_into_one_line_and_exit_1(monkeypatch, capsys):
    def boom(url, out_dir, today):
        raise ValueError("something odd\nsecond line")

    monkeypatch.setattr(fetch, "dispatch", boom)
    code = main(["https://youtu.be/zjkBMFhNj_g"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "fetch: unexpected ValueError: something odd second line\n"


def test_main_queue_mode_unexpected_exception_exit_1(monkeypatch, tmp_path, capsys):
    import fetch_queue

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fetch_queue, "run_queue", boom)
    code = main(["--queue", str(tmp_path / "q.md")])
    assert code == 1
    assert capsys.readouterr().err == "fetch: unexpected OSError: disk full\n"


def test_description_heading_lines_are_escaped():
    description = "Intro\n# Chapter 1\n   ## Part\n    # code block\nnot a #heading\n#hashtag"
    doc = render_document(_meta(description=description), Track("en", False), [], created=date(2026, 9, 16))
    section = doc.split("## Description\n\n", 1)[1].split("\n\n## Transcript", 1)[0]
    assert section == (
        "Intro\n\\# Chapter 1\n   \\## Part\n    # code block\nnot a #heading\n\\#hashtag"
    )
    headings = [line for line in doc.splitlines() if line.startswith("#")]
    assert headings == ['# Intro to "Large" Language Models', "## Description", "## Transcript"]


# --- oEmbed metadata and the hosted fallback (Supadata) --------------------------

from types import SimpleNamespace  # noqa: E402

import fetch_hosted  # noqa: E402
import yt_dlp  # noqa: E402
from youtube_transcript_api import IpBlocked, RequestBlocked, TranscriptsDisabled  # noqa: E402
from supadata.types import Transcript as SupaTranscript  # noqa: E402

HOSTED_ID = "qShed6dyrQY"
HOSTED_URL = f"https://youtu.be/{HOSTED_ID}?si=abc"
HOSTED_TODAY = date(2026, 10, 20)


def test_metadata_from_oembed_dates_today_and_marks_the_date_unknown():
    meta = fetch.metadata_from_oembed({"title": "Sample Video", "author_name": "Sample Channel"}, HOSTED_ID,
                                      HOSTED_TODAY, duration=754)
    assert meta == Metadata(title="Sample Video", channel="Sample Channel", upload_date=HOSTED_TODAY, duration=754,
                            description="", url=canonical_url(HOSTED_ID), language=None, video_id=HOSTED_ID,
                            published_known=False)


def test_metadata_from_an_empty_oembed_falls_back_to_the_id():
    meta = fetch.metadata_from_oembed({}, HOSTED_ID, HOSTED_TODAY)
    assert (meta.title, meta.channel) == (HOSTED_ID, "unknown")


def test_a_hosted_document_says_published_unknown_supadata_native_and_has_no_description():
    meta = fetch.metadata_from_oembed({"title": "T", "author_name": "C"}, HOSTED_ID, HOSTED_TODAY, duration=65)
    text = render_document(meta, Track("en", False), [Fragment("hi", 0.0)], HOSTED_TODAY,
                           transcription="supadata-native")
    assert "published: unknown\ncreated: 2026-10-20\n" in text
    assert 'duration: "1:05"\ncaptions: native\ntranscription: supadata-native\nlanguage: en\n' in text
    assert "## Description" not in text
    assert "# T\n\n## Transcript\n\n[0:00] hi\n" in text


class HostedClient:
    def __init__(self):
        self.calls = 0

    def transcript(self, url, lang=None, text=False, chunk_size=None, mode="auto"):
        self.calls += 1
        return SupaTranscript(content=[{"text": "from supadata", "offset": 0, "duration": 65000}], lang="en",
                              available_langs=["en"])


@pytest.fixture
def blocked(monkeypatch, tmp_path):
    """The local path blocked like a datacenter IP; the hosted client and oEmbed faked; no key anywhere."""
    state = {"error": yt_dlp.utils.DownloadError("ERROR: [youtube] x: Sign in to confirm you're not a bot")}
    client = HostedClient()

    def local_metadata(video_id):
        raise state["error"]

    monkeypatch.setattr(fetch, "fetch_metadata", local_metadata)
    monkeypatch.setattr(fetch_hosted, "make_client", lambda key: client)
    monkeypatch.setattr(fetch_hosted, "oembed", lambda video_id: {"title": "Sample Video", "author_name": "Sample Channel"})
    monkeypatch.setattr(fetch_hosted, "ENV_FILES", (tmp_path / "no.env",))
    monkeypatch.setattr(fetch_hosted, "spent", 0)
    monkeypatch.delenv("SUPADATA_API_KEY", raising=False)
    monkeypatch.delenv("SUPADATA_CREDITS_LEFT", raising=False)
    out = tmp_path / "raw"
    out.mkdir()
    return SimpleNamespace(state=state, client=client, out=out)


@pytest.mark.parametrize("error", [
    yt_dlp.utils.DownloadError("ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies"),
    IpBlocked(HOSTED_ID),
    RequestBlocked(HOSTED_ID),
])
def test_a_blocked_local_fetch_with_a_key_goes_to_the_hosted_api(blocked, monkeypatch, error):
    blocked.state["error"] = error
    monkeypatch.setenv("SUPADATA_API_KEY", "k")
    path = fetch.dispatch(HOSTED_URL, blocked.out, HOSTED_TODAY)
    assert path.name == "2026-10-20-sample-video.md"
    text = path.read_text(encoding="utf-8")
    assert 'author: "Sample Channel"\n' in text and f'video_id: "{HOSTED_ID}"\n' in text
    assert 'duration: "1:05"\ncaptions: native\ntranscription: supadata-native\n' in text
    assert "[0:00] from supadata" in text
    assert blocked.client.calls == 1
    assert fetch_hosted.spent == 1


def test_the_key_can_come_from_the_env_file(blocked, monkeypatch, tmp_path):
    env = tmp_path / "key.env"
    env.write_text("SUPADATA_API_KEY=k\n")
    monkeypatch.setattr(fetch_hosted, "ENV_FILES", (tmp_path / "no.env", env))
    assert fetch.dispatch(HOSTED_URL, blocked.out, HOSTED_TODAY).exists()


def test_without_a_key_the_block_is_raised_as_today(blocked):
    with pytest.raises(yt_dlp.utils.DownloadError):
        fetch.dispatch(HOSTED_URL, blocked.out, HOSTED_TODAY)
    assert blocked.client.calls == 0


@pytest.mark.parametrize("error", [
    yt_dlp.utils.DownloadError("ERROR: [youtube] x: Video unavailable"),
    FetchError("no usable captions; available: none", 1),
    TranscriptsDisabled(HOSTED_ID),
])
def test_other_errors_never_fall_back(blocked, monkeypatch, error):
    blocked.state["error"] = error
    monkeypatch.setenv("SUPADATA_API_KEY", "k")
    with pytest.raises(type(error)):
        fetch.dispatch(HOSTED_URL, blocked.out, HOSTED_TODAY)
    assert blocked.client.calls == 0


def test_the_quota_guard_stops_before_calling_the_hosted_api(blocked, monkeypatch):
    monkeypatch.setenv("SUPADATA_API_KEY", "k")
    monkeypatch.setenv("SUPADATA_CREDITS_LEFT", "1")
    fetch.dispatch(HOSTED_URL, blocked.out, HOSTED_TODAY)
    with pytest.raises(fetch_hosted.HostedQuota) as exc:
        fetch.dispatch("https://youtu.be/L09faAUZsho", blocked.out, HOSTED_TODAY)
    assert exc.value.exit_code == 4
    assert "hosted credits used up" in str(exc.value)
    assert blocked.client.calls == 1


def test_a_blocked_caption_list_after_good_metadata_also_falls_back(blocked, monkeypatch):
    class BlockedCaptions:
        def __init__(self, video_id):
            raise RequestBlocked(video_id)

    monkeypatch.setattr(fetch, "fetch_metadata", lambda video_id: _meta(video_id=HOSTED_ID))
    monkeypatch.setattr(fetch, "YouTubeCaptions", BlockedCaptions)
    monkeypatch.setenv("SUPADATA_API_KEY", "k")
    assert fetch.dispatch(HOSTED_URL, blocked.out, HOSTED_TODAY).exists()
