# youtube-transcript-fallback

[![Powered by Supadata](https://img.shields.io/badge/Powered%20by-Supadata-6E56CF)](https://supadata.ai)
[![test](https://github.com/cgorricho/youtube-transcript-fallback/actions/workflows/test.yml/badge.svg)](https://github.com/cgorricho/youtube-transcript-fallback/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Fetch YouTube captions into markdown files, one file per video, and keep fetching when YouTube
blocks your IP.

- **YouTube first.** Title, channel, date and description come from `yt-dlp`; the captions from
  `youtube-transcript-api`, manual tracks before auto-generated ones, English or Spanish.
- **[Supadata](https://supadata.ai) when YouTube blocks the IP.** YouTube refuses caption requests
  from most datacenter IPs ("Sign in to confirm you're not a bot", `IpBlocked`, `RequestBlocked`).
  With `SUPADATA_API_KEY` set, a blocked fetch asks Supadata for the video's own captions
  (`mode=native`, never an AI transcript, so one video costs one credit), through Supadata's
  official Python SDK. Without the key, a block fails as usual.
- **A queue with a dedupe log.** Paste links into `fetch-queue.md`; `fetch.py --queue` fetches each
  new one, records every attempt in `fetch-log.tsv`, removes the fetched lines, commits and pushes.
  A video already in the log or in `raw/` is never fetched twice, so several machines can share one
  git repository.
- **A scheduler with backoff and a credit guard.** `schedule_fetch.py` pulls every git clone under
  a folder and runs the queue in each, at most 8 links a run, with growing, jittered pauses. After a
  YouTube block it skips runs for 30 minutes, then 1, 2 and 4 hours. It reads your Supadata balance
  once a run from `/v1/me` and stops before the credits run out; the YouTube links wait in the queue.

## Install

Needs [uv](https://docs.astral.sh/uv/) and git.

    git clone https://github.com/cgorricho/youtube-transcript-fallback.git
    cd youtube-transcript-fallback
    uv sync
    cp .env.example .env        # then put your Supadata key after SUPADATA_API_KEY=

The key is optional: without it everything works except the fallback. Get one at
[supadata.ai](https://supadata.ai); the free plan gives 100 credits a month. `.env` is gitignored.
The SDK is pinned (`supadata==1.6.0`); `tests/test_fetch_hosted_sdk.py` drives the real SDK client
over a fake transport, so a version change that breaks the integration fails the tests.

## Usage

### One video

    uv run python fetch.py https://youtu.be/VIDEO_ID --out raw

Writes `raw/<upload-date>-<title-slug>.md` and prints its path. The file has YAML frontmatter in the
Obsidian Web Clipper style (`title`, `author`, `source`, `published`, `created`,
`type: youtube-transcript`, `video_id`, `duration`, `captions: manual | auto | native`, `language`,
`tags: clippings`), then the description and the transcript, one paragraph per minute opened by its
timestamp. A file fetched through Supadata says `published: unknown` (the date is the day fetched),
`captions: native`, `transcription: supadata-native`, and has no description. An existing file is
never overwritten.

Exit codes: 0 done, 1 failed (no usable captions, network), 2 bad input (not a YouTube video URL).

### A queue

A workspace is a git repository with a `raw/` folder and a `fetch-queue.md`:

    # Fetch queue
    - https://www.youtube.com/watch?v=VIDEO_ID
    https://youtu.be/OTHER_ID
    <https://www.youtube.com/shorts/SHORT_ID>

Then, inside the workspace:

    uv run --project /path/to/youtube-transcript-fallback \
      python /path/to/youtube-transcript-fallback/fetch.py --queue fetch-queue.md

Options: `--max N` (fetch at most N new videos), `--pause S` (seconds before the second video,
default 10; each later pause is 25% longer, up to 180 s, varied by up to 25%), `--no-push` (no
commit and push), `--out DIR` (default `raw`).

Fetched and duplicate lines leave the queue; failed lines, comments, notes and links to other sites
stay. `fetch-log.tsv` gets one row per attempt: `date`, `video_id`, `result` (`fetched`,
`duplicate`, `failed`), `raw_file`, `url`, `note` (`hosted` for a Supadata fetch, or the failure
reason). Three failures in a row stop the run.

Exit codes: 0 done, 1 some links failed, 2 stopped after three failures in a row, 3 YouTube blocked
the connection or Supadata refused the key (stop, retry later), 4 the Supadata credits are used up
(the YouTube links wait; nothing is logged for them).

### The scheduler

    uv run --project /path/to/youtube-transcript-fallback \
      python /path/to/youtube-transcript-fallback/schedule_fetch.py --root /path/to/workspaces

For every git clone directly under `--root` (and each `--repo PATH`, repeatable, for clones that
live elsewhere): `git pull --ff-only`, then, when `fetch-queue.md` holds a link, the queue run
above. One line per clone goes to the log (`--log`, default `<root>/fetch-schedule.log`). Exit 0
when no clone failed, 1 otherwise.

- **Per-run cap.** At most `--per-run` links (default 8) across all clones.
- **Backoff.** When a clone's run reports a YouTube block (exit 3), the run stops and the next runs
  are skipped: 30 minutes, then 1 h, 2 h, 4 h at most, each stretched by up to a quarter at random.
  A run that fetches without a block clears it. The state is `fetch-backoff.json` beside the log.
- **Credit guard.** With `SUPADATA_API_KEY` in the environment or in `--env-file` (default: `.env`
  beside the script), each fetch gets the key and `SUPADATA_CREDITS_LEFT`. At the first clone whose
  queue holds a YouTube link, the run reads the balance from Supadata's `/v1/me` once, so bonus
  credits count; each clone gets that balance minus the hosted rows the run's earlier clones
  logged. When `/me` fails, the guard falls back to `--hosted-quota` (default 100, the free plan)
  minus this month's hosted rows in every clone's `fetch-log.tsv`. The log says
  `hosted credits: N left of M (supadata)` or
  `hosted credits: N left of M (local count; supadata /me: <error>)`. If Supadata itself answers
  `limit-exceeded`, the run stops the same way: exit 4, links wait, no backoff. The key goes only
  into the child's environment, never into arguments or the log.

### Cron

Every 30 minutes, one run at a time:

    */30 * * * * flock -n /tmp/yt-fetch.lock sh -c 'cd /path/to/youtube-transcript-fallback && uv run python schedule_fetch.py --root /path/to/workspaces --log /path/to/logs/yt-fetch.log' >> /path/to/logs/yt-fetch.cron.log 2>&1

The workspaces need git credentials that can push without a prompt (an SSH key or a credential
helper).

## Tests

    uv run pytest -q

No test touches the network: YouTube, Supadata and git are faked.

## License

MIT, see [LICENSE](LICENSE).
