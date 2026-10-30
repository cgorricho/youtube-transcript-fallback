"""Fetch a YouTube video's captions into raw/ as a clipper-style markdown file.

The local path first (yt-dlp metadata, youtube-transcript-api captions); when YouTube blocks the
connection and SUPADATA_API_KEY is set, the hosted fallback in fetch_hosted.py. `--queue` runs
fetch_queue.py over a queue file. See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from youtube_transcript_api import (
    IpBlocked,
    RequestBlocked,
    YouTubeTranscriptApi,
    YouTubeTranscriptApiException,
)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
ACCEPTED_LANGUAGES = ("en", "es")

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class FetchError(Exception):
    """A user-facing failure. exit_code becomes the process exit status."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Track:
    language_code: str
    is_generated: bool


@dataclass(frozen=True)
class Fragment:
    text: str
    start: float


@dataclass(frozen=True)
class Metadata:
    title: str
    channel: str
    upload_date: date
    duration: int
    description: str
    url: str
    language: str | None
    video_id: str
    published_known: bool = True    # False: oEmbed metadata, upload_date is the day fetched


def video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in YOUTUBE_HOSTS:
        raise FetchError(f"unsupported host: {host or url}", 2)
    candidate = None
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    else:
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] == "watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed"):
            candidate = parts[1]
    if not candidate or not _VIDEO_ID.match(candidate):
        raise FetchError(f"not a recognised YouTube URL: {url}", 2)
    return candidate


def base_language(code: str) -> str:
    return code.split("-")[0].lower()


def select_track(tracks: list[Track], spoken: str | None = None) -> Track:
    """Manual en/es first (spoken language preferred), then auto en/es, else fail."""
    preferred = list(ACCEPTED_LANGUAGES)
    if spoken and base_language(spoken) in preferred:
        preferred.remove(base_language(spoken))
        preferred.insert(0, base_language(spoken))

    def find(generated: bool) -> Track | None:
        for lang in preferred:
            for track in tracks:
                if track.is_generated == generated and base_language(track.language_code) == lang:
                    return track
        return None

    chosen = find(False) or find(True)
    if chosen is None:
        available = ", ".join(
            f"{t.language_code} ({'auto' if t.is_generated else 'manual'})" for t in tracks
        ) or "none"
        raise FetchError(f"no usable captions; available: {available}", 1)
    return chosen


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_transcript(fragments: list[Fragment]) -> str:
    """One paragraph per minute of video, opened by the first fragment's timestamp."""
    paragraphs: list[str] = []
    current_minute: int | None = None
    marker = ""
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            paragraphs.append(f"[{marker}] {' '.join(buffer)}")
            buffer.clear()

    for fragment in fragments:
        minute = int(fragment.start // 60)
        text = " ".join(fragment.text.split())
        if not text:
            continue
        if minute != current_minute:
            flush()
            current_minute = minute
            marker = format_timestamp(fragment.start)
        buffer.append(text)
    flush()
    return "\n\n".join(paragraphs)


def slugify(title: str, max_length: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > max_length:
        cut = slug[:max_length]
        if slug[max_length] != "-" and "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        slug = cut.rstrip("-")
    return slug or "untitled"


def raw_filename(upload_date: date, title: str) -> str:
    return f"{upload_date.isoformat()}-{slugify(title)}.md"


def yaml_string(value: str) -> str:
    """Double-quoted YAML scalar. JSON string syntax is valid YAML."""
    return json.dumps(value, ensure_ascii=False)


_HEADING_START = re.compile(r"^( {0,3})#", re.MULTILINE)


def escape_headings(text: str) -> str:
    """Backslash-escape a `#` that would open a markdown heading, so the raw file's
    outline stays the tool's own (title, Description, Transcript)."""
    return _HEADING_START.sub(r"\1\\#", text)


# The Obsidian Web Clipper's tag, written the way the clipper writes it, so a tag search
# finds fetched and clipped raw files alike.
CLIPPINGS_TAG = ["tags:", '  - "clippings"']


def render_document(meta: Metadata, track: Track, fragments: list[Fragment], created: date,
                    transcription: str | None = None) -> str:
    """transcription set ("supadata-native"): the video's own captions through the hosted API,
    whether manual or auto unknown; no description, which oEmbed does not give."""
    if transcription:
        captions = "native"
    else:
        captions = "auto" if track.is_generated else "manual"
    description = [] if transcription else [
        "## Description",
        "",
        escape_headings(meta.description.strip()) or "(none)",
        "",
    ]
    lines = [
        "---",
        f"title: {yaml_string(meta.title)}",
        f"author: {yaml_string(meta.channel)}",
        f"source: {yaml_string(meta.url)}",
        f"published: {meta.upload_date.isoformat() if meta.published_known else 'unknown'}",
        f"created: {created.isoformat()}",
        "type: youtube-transcript",
        f"video_id: {yaml_string(meta.video_id)}",
        f'duration: "{format_timestamp(meta.duration)}"',
        f"captions: {captions}",
        *([f"transcription: {transcription}"] if transcription else []),
        f"language: {base_language(track.language_code)}",
        *CLIPPINGS_TAG,
        "---",
        "",
        f"# {meta.title}",
        "",
        *description,
        "## Transcript",
        "",
        render_transcript(fragments),
        "",
    ]
    return "\n".join(lines)


def write_raw(out_dir: Path, filename: str, content: str) -> Path:
    if not out_dir.is_dir():
        raise FetchError(f"output directory does not exist: {out_dir}", 1)
    target = out_dir / filename
    try:
        # "x" creates the file only if it does not exist, atomically: no check-then-write race.
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        raise FetchError(f"refusing to overwrite {target}", 1) from None
    return target


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def metadata_from_info(info: dict, video_id: str, today: date) -> Metadata:
    """Pure mapping from a yt-dlp info dict to Metadata. today is the fallback upload date."""
    upload = info.get("upload_date")  # YYYYMMDD or None
    if upload:
        upload_date = date(int(upload[:4]), int(upload[4:6]), int(upload[6:8]))
    else:
        upload_date = today
    return Metadata(
        title=info.get("title") or video_id,
        channel=info.get("channel") or info.get("uploader") or "unknown",
        upload_date=upload_date,
        duration=int(info.get("duration") or 0),
        description=info.get("description") or "",
        url=info.get("webpage_url") or canonical_url(video_id),
        language=info.get("language"),
        video_id=video_id,
    )


def metadata_from_oembed(data: dict, video_id: str, today: date, duration: int = 0) -> Metadata:
    """Pure mapping from YouTube's oEmbed answer: no upload date, so today, marked unknown."""
    return Metadata(
        title=data.get("title") or video_id,
        channel=data.get("author_name") or "unknown",
        upload_date=today,
        duration=duration,
        description="",
        url=canonical_url(video_id),
        language=None,
        video_id=video_id,
        published_known=False,
    )


def fetch_metadata(video_id: str) -> Metadata:
    """Network call. Title, channel, date, duration, description via yt-dlp, no download."""
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(canonical_url(video_id), download=False)
    if not info.get("upload_date"):
        print("fetch: no upload date from yt-dlp; using today", file=sys.stderr)
    return metadata_from_info(info, video_id, date.today())


class YouTubeCaptions:
    """Network call on construction: lists caption tracks once, fetches on demand."""

    def __init__(self, video_id: str):
        self._transcripts = YouTubeTranscriptApi().list(video_id)
        self.tracks = [Track(t.language_code, t.is_generated) for t in self._transcripts]

    def fetch(self, track: Track) -> list[Fragment]:
        for transcript in self._transcripts:
            if transcript.language_code == track.language_code and transcript.is_generated == track.is_generated:
                return [Fragment(s.text, s.start) for s in transcript.fetch()]
        raise FetchError(f"caption track not found: {track}", 1)


_BOT_CHECK = ("confirm you're not a bot", "confirm you’re not a bot")


def is_youtube_block(exc: BaseException) -> bool:
    """YouTube refusing this connection (a datacenter or flagged IP), not a problem with the video."""
    if isinstance(exc, (IpBlocked, RequestBlocked)):
        return True
    return isinstance(exc, yt_dlp.utils.DownloadError) and any(s in str(exc).lower() for s in _BOT_CHECK)


def fetch_youtube(url: str, out_dir: Path, today: date) -> Path:
    """The local path first; on a YouTube block, with SUPADATA_API_KEY set, the hosted API."""
    video_id = video_id_from_url(url)
    meta = None
    try:
        meta = fetch_metadata(video_id)
        captions = YouTubeCaptions(video_id)
        track = select_track(captions.tracks, meta.language)
    except Exception as exc:
        if not is_youtube_block(exc):
            raise
        import fetch_hosted

        settings = fetch_hosted.settings_from_env(os.environ, fetch_hosted.ENV_FILES)
        if settings is None:
            raise
        print(f"fetch: YouTube blocked this connection ({type(exc).__name__}); "
              "fetching the captions through Supadata", file=sys.stderr)
        return hosted_youtube(video_id, out_dir, today, settings, meta)
    fragments = captions.fetch(track)
    content = render_document(meta, track, fragments, today)
    return write_raw(out_dir, raw_filename(meta.upload_date, meta.title), content)


def hosted_youtube(video_id: str, out_dir: Path, today: date, settings, meta: Metadata | None = None) -> Path:
    """The hosted fallback: captions from Supadata, title and channel from oEmbed unless the local
    metadata call already answered. One credit per call; the quota guard is checked first."""
    import fetch_hosted

    if settings.credits_left is not None and fetch_hosted.spent >= settings.credits_left:
        raise fetch_hosted.HostedQuota("hosted credits used up; YouTube links wait")
    fetch_hosted.spent += 1
    transcript = fetch_hosted.hosted_transcript(video_id, fetch_hosted.make_client(settings.key))
    if meta is None:
        meta = metadata_from_oembed(fetch_hosted.oembed(video_id), video_id, today, transcript.duration)
    content = render_document(meta, Track(transcript.lang, False), transcript.fragments, today,
                              transcription="supadata-native")
    return write_raw(out_dir, raw_filename(meta.upload_date, meta.title), content)


def dispatch(url: str, out_dir: Path, today: date) -> Path:
    """A YouTube URL, checked with no network call; anything else exits 2."""
    host = urlparse(url).hostname or ""
    if host in YOUTUBE_HOSTS:
        return fetch_youtube(url, out_dir, today)
    raise FetchError(f"unsupported host: {host or url}", 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch a YouTube video's captions into raw/ as a clipper-style markdown file."
    )
    parser.add_argument("url", nargs="?", help="a YouTube URL")
    parser.add_argument("--out", default="raw", help="output directory (default: raw)")
    parser.add_argument("--queue", metavar="FILE", help="fetch every link in a queue file (fetch-many)")
    parser.add_argument("--pause", type=float, default=10,
                        help="seconds before the second video with --queue; each later pause is 25%% longer, up to 180 s, "
                        "and varied by up to 25%% (default: 10)")
    parser.add_argument("--max", type=int, dest="max_new", metavar="N", help="with --queue, fetch at most N new videos")
    parser.add_argument("--no-push", action="store_true", help="with --queue, do not commit and push")
    args = parser.parse_args(argv)
    if (args.url is None) == (args.queue is None):
        parser.error("give either a URL or --queue FILE")
    try:
        if args.queue is not None:
            return run_queue_command(args)
        return run_url_command(args)
    except Exception as exc:  # anything unforeseen: one line, not a traceback
        message = " ".join(str(exc).split())
        print(f"fetch: unexpected {type(exc).__name__}: {message}".rstrip(), file=sys.stderr)
        return 1


def run_url_command(args: argparse.Namespace) -> int:
    try:
        path = dispatch(args.url, Path(args.out), date.today())
    except FetchError as exc:
        print(f"fetch: {exc}", file=sys.stderr)
        return exc.exit_code
    except (IpBlocked, RequestBlocked) as exc:
        print(
            f"fetch: {type(exc).__name__}: YouTube blocked the request. "
            "Datacenter IPs are refused; run from a residential connection.",
            file=sys.stderr,
        )
        return 1
    except (YouTubeTranscriptApiException, yt_dlp.utils.DownloadError) as exc:
        print(f"fetch: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def run_queue_command(args: argparse.Namespace) -> int:
    import fetch_queue

    queue = Path(args.queue)
    out_dir = Path(args.out)
    today = date.today()
    try:
        result = fetch_queue.run_queue(
            queue, out_dir, queue.parent / "fetch-log.tsv",
            lambda url: dispatch(url, out_dir, today),
            today=today, pause=args.pause, max_new=args.max_new, push=not args.no_push,
        )
    except FetchError as exc:
        print(f"fetch: {exc}", file=sys.stderr)
        return exc.exit_code
    return result.exit_code


if __name__ == "__main__":
    # Run from the importable module, so fetch_queue and this entry point share one
    # FetchError class (a script is otherwise loaded twice: as __main__ and as fetch).
    import fetch

    sys.exit(fetch.main())
