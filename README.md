# immich-tools

Small scripts for the [Immich](https://immich.app) API built around its v3 search API.

## Shared code (`immich_api.py`)

All three scripts take their HTTP plumbing from `immich_api.py`: the API client with retry/backoff,
`normalize_url`, the `Logger`, `ImmichError` and the argparse type helpers. It is a module, not a
script - the PEP 723 header and the CLI stay in the scripts, so `uv run <script>.py` is enough.
Each script narrows the permissions named in an HTTP 403 message to the ones it actually needs.

## `conditional_albums.py`

Creates and syncs Immich albums from saved search payloads. The input is a JSON object that maps
an album name to one or more payloads - the body the Immich frontend POSTs to
`/api/search/metadata`:

```json
{
  "Sommer 1996": [
    {"visibility": "timeline", "page": 1, "withExif": true,
     "takenAfter": "1995-01-01T00:00:00.000Z",
     "takenBefore": "1997-12-31T23:59:59.999Z", "isFavorite": true}
  ],
  "Ostfildern": [
    {"filter": {"city": {"eq": "Ostfildern"}}}
  ]
}
```

Every album in the file is processed in order. The union of all its search results is the desired
content, so a run

- creates the album when no owned album with that exact name exists,
- adds the matching assets that are not in the album yet,
- removes the assets that are in the album but no longer match (`--no-remove` keeps them).

### Behaviour worth knowing

- Payloads are passed through untouched. Only `size` is set (`--page-size`) and pagination is
driven by the script: `cursor` for the structured v3 shape, `page`/`nextPage` for the deprecated
flat shape. A `page` next to `filter`/`orderBy` is dropped with a warning, because the server
rejects that combination with a 400.
- The album content is read with a search on `filter.albumIds.any` plus `trashedAt: {eq: null}`,
because v3 album responses carry no assets array. That filter is album-confined, so it also
returns assets owned by other users in a shared album.
- Assets that are invisible to search (locked, trashed) are never removed. When an album holds
more assets than the search can see, the difference is reported as a warning.
- **Planning is all-or-nothing**: if any album fails to plan (bad JSON, API error, ambiguous
name) the run stops with exit code `2` and nothing is written. The apply phase is per album - a
failing album does not stop the others and the run ends with exit code `1`.
- Album names are matched exactly (case-sensitive) and only among owned albums. Two owned albums
with the same name abort the run instead of guessing.
- Only asset **metadata** (JSON) is fetched - image bytes are never downloaded.
- Nothing is ever deleted: the script creates albums and changes album membership only. Assets are
never deleted, trashed, uploaded or modified, and an album that exists is never deleted or renamed.
- **Dry run by default**: nothing is written unless `--apply` is passed. `album.delete` is not needed (the script never deletes an album).

### Usage

```bash
export IMMICH_URL=https://immich.example.com
export IMMICH_API_KEY=xxxxxxxx

# dry run: what would change per album (default, writes nothing)
uv run conditional_albums.py --file albums.json

# apply and keep a report of every change
uv run conditional_albums.py --file albums.json --apply --json-report albums-report.json

# inline JSON, only add, never remove
uv run conditional_albums.py --json '{"Test": [{"filter": {"isFavorite": {"eq": true}}}]}' --apply --no-remove
```

`uv run` does **not** read `.env` on its own - pass it explicitly
(`uv run --env-file .env conditional_albums.py --file albums.json` or
`UV_ENV_FILE=.env uv run ...`). Exported variables take precedence.

Exit codes: `0` success (or nothing to do), `1` at least one album update failed, `2`
configuration or API error before any write.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--file PATH` / `--json JSON` | - | exactly one is required: the specification |
| `--url`, `--api-key` | env `IMMICH_URL`, `IMMICH_API_KEY` | server and credentials |
| `--apply` | off | actually write; otherwise dry run |
| `--no-remove` | off | only add; leave non-matching assets in the album |
| `--page-size N` | `250` | search page size, 1-1000 |
| `--batch-size N` | `500` | assets per album update request, **not** a cap |
| `--json-report PATH` | - | one entry per album: `added`, `removed`, `errors`, `failures` |
| `--retries N`, `--timeout S` | `3`, `30` | retry/backoff and request timeout |
| `--insecure` | off | skip TLS verification (self-signed homelab certs) |
| `-q`, `-v` | - | quiet progress / log every HTTP request (errors are always printed) |

## `favorite_rated.py`

Finds images with a star rating >= `--min-rating` (default 3) that are **not** favorited yet and sets the favorite flag.

- Only asset **metadata** (JSON) is fetched - image bytes are never downloaded.
- **Dry run by default**: nothing is written unless `--apply` is passed.
- Uses the current Immich search API (structured `filter` + cursor pagination, v3.2.0+) and `PATCH /api/assets`.

### Requirements

- Immich **3.2.0 or newer** (older servers are rejected with a clear message).
- An API key with permissions: `asset.read`, `asset.update`, `user.read`.
- [uv](https://docs.astral.sh/uv/) - the script carries PEP 723 metadata, so `uv run` fetches nothing but Python itself.

### Usage

```bash
export IMMICH_URL=https://immich.example.com
export IMMICH_API_KEY=xxxxxxxx

# dry run: list what would be favorited (default, writes nothing)
uv run favorite_rated.py

# canary: favorite 5 assets, keeping a revert report
uv run favorite_rated.py --limit 5 --apply --json-report favorites.json

# full run, rating threshold 4
uv run favorite_rated.py --min-rating 4 --apply

# undo a previous run
uv run favorite_rated.py --revert favorites.json --apply
```

`--url`/`--api-key` can be passed as flags instead of environment variables.

`uv run` does **not** read `.env` on its own - pass it explicitly (needed if you keep the
credentials in `.env`, which is gitignored here):

```bash
uv run --env-file .env favorite_rated.py     # or: UV_ENV_FILE=.env uv run favorite_rated.py
```

Variables already exported in the shell take precedence over values from `.env`.

### What is searched

```json
{
  "filter": {
    "type": { "eq": "IMAGE" },
    "rating": { "gte": 3 },
    "isFavorite": { "eq": false },
    "trashedAt": { "eq": null }
  },
  "orderBy": { "field": "fileCreatedAt", "direction": "desc" },
  "withExif": true,
  "size": 250
}
```

Archived images are included, trashed and locked ones are not. Because search spans partner
libraries while `PATCH /api/assets` only accepts assets you own, every result is re-checked
client side (`ownerId`, `isFavorite`, `rating`) before it is written.

### Failure behavior

A run is idempotent and resumable: search only returns assets with `isFavorite: false`, so
re-running after any failure picks up exactly what is left.

- **Setup errors abort before anything is written** (exit code `2`): unreachable server,
  Immich older than 3.2.0, a failing `GET /api/users/me`, or a failing search request.
- **Update failures are per batch and non-fatal**: every `PATCH /api/assets` is attempted
  independently. A failing batch is logged, its ids are skipped, and the remaining batches
  still run (exit code `1`).
- There is **no circuit breaker**: a wrong/expired API key or a missing `asset.update`
  permission fails *every* batch, so the run keeps printing failures for all remaining
  batches before exiting. The error message names the permission that is likely missing.
- A partially failed run prints `favorited: <n>` where `<n>` excludes the failed batches, and
  one `failed:` line per failed batch, so the number is always the confirmed writes.
- `--json-report` is written even when batches fail and lists **attempted** assets (it is the
  candidate list, not a confirmation). Reverting such a report is harmless - setting
  `isFavorite: false` on an already-unfavorited asset is a no-op.
- `Ctrl-C` is not intercepted: the run stops mid-flight and batches already written stay
  written. Re-run to continue.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--url`, `--api-key` | env `IMMICH_URL`, `IMMICH_API_KEY` | server and credentials |
| `--min-rating N` | `3` | minimum star rating (1-5) |
| `--apply` | off | actually write; otherwise dry run |
| `--limit N` | - | stop after N candidates; the only cap on a run |
| `--page-size N` | `250` | search page size, 1-1000 (pagination follows the cursor, so it is not a cap) |
| `--batch-size N` | `500` | assets per `PATCH` request, **not** a cap: 1200 candidates = 3 requests (500/500/200) |
| `--json-report PATH` | - | write affected assets for later `--revert` |
| `--revert PATH` | - | remove the favorite flag from a report's assets |
| `--retries N`, `--timeout S` | `3`, `30` | retry/backoff and request timeout |
| `--insecure` | off | skip TLS verification (self-signed homelab certs) |
| `-q`, `-v` | - | quiet progress / log every HTTP request (errors are always printed, even with `-q`) |

Exit codes: `0` success (or nothing to do), `1` at least one update batch failed,
`2` configuration or API error before any write.

## `external_library_scan.py`

Scans Immich **external libraries**: one `POST /api/libraries/{id}/scan` per library (every library
by default, or the ones named with `--library`), optionally following the work with `--wait`.
`--status` reports the scanner queue and the libraries instead of scanning anything.

- **Dry run by default**: nothing is queued unless `--apply` is passed.
- Every library is listed with its media counts (`GET /api/libraries/{id}/statistics`) and with its
  `refreshedAt` / `updatedAt` timestamps, so `--status` answers "is a scan due?" and a scan run
  shows what was known before it started. `refreshedAt` is `never` for a library that has not been
  scanned yet.
- The `assetCount` of `GET /api/libraries` is **not** used: Immich does not populate it for external
  libraries (it reads `0` while the statistics endpoint reports the real totals).
- Only current endpoints are used: `POST /api/libraries/{id}/scan`, `GET /api/libraries`,
  `GET /api/libraries/{id}/statistics` and `GET /api/queues/library`. The deprecated
  `PUT /api/jobs/{queue}` scan-all command is not used, so "all libraries" is a per-library fan-out
  (N libraries = N requests).
- Unlike that scan-all job, a per-library scan does **not** queue the cleanup of libraries stuck in
  deletion - the nightly cron job still handles those.
- Waiting watches the `library` queue, which is where the disk crawl and the asset check run.
  Newly imported files keep `sidecar` and `metadataExtraction` busy afterwards; those are left alone.

### Requirements

- An **admin** API key: `library.read`, `library.update`, `library.statistics`, `queue.read` (every
  one of those endpoints is admin-only in Immich).
- [uv](https://docs.astral.sh/uv/) - the script carries PEP 723 metadata, so `uv run` fetches
  nothing but Python itself.

### Usage

```bash
export IMMICH_URL=https://immich.example.com
export IMMICH_API_KEY=xxxxxxxx

# dry run: list what would be scanned (default, queues nothing)
uv run external_library_scan.py

# scan every external library and wait for the scanner queue to drain
uv run external_library_scan.py --apply --wait

# scan single libraries (uuid or exact name, repeatable)
uv run external_library_scan.py --apply --library Photos --library 59f55eb0-32e5-4037-b53e-5e41c1f2d9b3

# where is the scanner right now, and how fresh are the libraries? (read-only)
uv run external_library_scan.py --status

# follow a scan that is already running, e.g. the nightly cron job
uv run external_library_scan.py --status --wait

# machine-readable, for cron jobs and monitoring
uv run external_library_scan.py --status --json
```

### Behaviour worth knowing

- `--library` values are resolved against `GET /api/libraries` **before** any request is sent, so a
  typo aborts with exit code `2` and nothing is queued. A value is either a full uuid
  (case-insensitive) or an exact library name; a name that matches several libraries is rejected
  with the candidate ids, and repeated selectors are de-duplicated.
- Media counts and timestamps are read **before** the scans start, so the reported numbers describe
  the state the run started from (one `GET .../statistics` per reported library, none for libraries
  that `--library` excluded).
- When `--wait` runs to completion the libraries are read **again**, and each one is then printed as
  `REFRESHED ... refreshed=<before> -> <after>` - the proof that the scan actually refreshed it.
  Nothing is re-read without `--wait`, and nothing is re-read when the wait timed out, was paused or
  the scan request had failed. `--status --wait` does the same re-read, so it shows what changed
  while you were watching (for example a nightly cron scan).
- A failing statistics call only degrades that one library: the line says `media counts unknown`,
  the reason is logged as a warning on stderr, and the run continues - the exit code is unaffected,
  because counts are reporting, not work.
- Timestamps are shown as UTC (`refreshed=2026-09-19 14:18:34Z`, fractional seconds dropped); the
  JSON report keeps the raw API strings. `refreshed=never` means the library was never scanned.
- A failing scan request does not stop the others: the remaining libraries are still requested, the
  failing one is named on stderr, and the exit code is `1`.
- A scan only counts as queued once its `POST` succeeded - the script never reports work it did not
  start.
- Waiting considers the queue finished when `active + waiting + delayed + paused` is `0`. A
  **paused** library queue can never drain, so the wait stops with exit code `1` and a hint to
  resume it in Administration -> Queues.
- `--wait-timeout` (default one hour, `0` waits forever) bounds the wait; while waiting, the
  per-poll lines are only shown with `-v`.
- Job counts in the status line and in the final summary are **cumulative for the queue**, not for
  this run: `completed` only ever grows and a `failed` count can predate the scan you just started.
- `--json` prints a single JSON report on stdout and moves the human-readable lines to stderr, so
  stdout stays parseable.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--url`, `--api-key` | env `IMMICH_URL`, `IMMICH_API_KEY` | server and credentials (admin key) |
| `--status` | off | report the `library` queue instead of scanning |
| `--library ID\|NAME` | - | scan only this library; repeatable, all libraries when omitted |
| `--apply` | off | actually start the scans; otherwise dry run |
| `--wait` | off | poll until the `library` queue is idle |
| `--poll-interval S` | `5` | seconds between queue status polls |
| `--wait-timeout S` | `3600` | give up waiting after SECONDS; `0` waits forever |
| `--json` | off | write one JSON report on stdout and nothing else |
| `--retries N`, `--timeout S` | `3`, `30` | retry/backoff and request timeout |
| `--insecure` | off | skip TLS verification (self-signed homelab certs) |
| `-q`, `-v` | - | quiet progress / log every HTTP request (errors are always printed, even with `-q`) |

`--status` and `--library` cannot be combined.

Exit codes: `0` success (including a dry run, and a scan that drained), `1` at least one scan
request failed or the wait timed out / hit a paused queue, `2` configuration, selector or API error
before anything was queued.

```console
$ uv run external_library_scan.py --status
queue library: isPaused=false active=0 waiting=0 delayed=0 paused=0 failed=1 completed=3
libraries: 2
  ds photo (59f55eb0-32e5-4037-b53e-5e41c1f2d9b3) - 54027 media (52958 photos, 1069 videos), refreshed=2026-09-19 14:18:34Z updated=2026-09-19 14:18:34Z
  ds video (5fe12488-14af-4547-ab48-59014894f0ca) - 0 media (0 photos, 0 videos), refreshed=never updated=2026-09-19 14:18:34Z

$ uv run external_library_scan.py --apply --wait
  QUEUED ds photo (59f55eb0-32e5-4037-b53e-5e41c1f2d9b3) - 0 media (0 photos, 0 videos), refreshed=2026-09-01 00:00:00Z updated=2026-09-01 00:00:00Z
scan queued: 1 of 1
waiting for queue library to finish (poll 5s, timeout 3600)
queue library is idle after 128s - 412 completed, 1 failed (queue totals)
  REFRESHED ds photo (59f55eb0-32e5-4037-b53e-5e41c1f2d9b3) - 54027 media (52958 photos, 1069 videos), refreshed=2026-09-01 00:00:00Z -> 2026-09-19 14:18:34Z
```

The `--json` report keeps the API's own field names:

```json
{
  "tool": "external_library_scan",
  "generated_at": "2026-09-19T18:20:00+0200",
  "server": "https://immich.example.com",
  "queue": "library",
  "applied": true,
  "started": true,
  "libraries": [
    {
      "id": "59f55eb0-32e5-4037-b53e-5e41c1f2d9b3",
      "name": "ds photo",
      "refreshedAt": "2026-09-19T14:18:34.975Z",
      "updatedAt": "2026-09-19T14:18:34.978Z",
      "statistics": { "photos": 52958, "videos": 1069, "usage": 247135360484, "total": 54027 },
      "statisticsError": null,
      "scanQueued": true,
      "scanError": null,
      "after": {
        "refreshedAt": "2026-09-19T14:18:34.975Z",
        "updatedAt": "2026-09-19T14:18:34.978Z",
        "statistics": { "photos": 52958, "videos": 1069, "usage": 247135360484, "total": 54027 },
        "statisticsError": null
      }
    }
  ],
  "queueState": {
    "name": "library",
    "isPaused": false,
    "statistics": { "active": 1, "completed": 412, "failed": 0, "delayed": 0, "waiting": 0, "paused": 0 }
  },
  "waited": true,
  "elapsed_seconds": 128.4
}
```

`refreshedAt`, `updatedAt` and `statistics` come straight from the API (`statistics` is `null` with a
`statisticsError` when that call failed). `after` holds the same four fields read once the wait
finished, and is `null` when there was no `--wait`, the wait failed, or the library was never queued.
`queueState` is the verbatim `GET /api/queues/library` response, or `null` when neither `--status`
nor `--wait` asked for it. `scanQueued` / `scanError` are only meaningful in scan mode, so they stay
`false` / `null` for a `--status` run.

## Tests

```bash
uv run tests/test_favorite_rated.py
uv run tests/test_conditional_albums.py
uv run tests/test_external_library_scan.py
```

All three suites fake the HTTP layer, so they never touch a real server. The failure paths above are
covered too, e.g. `test_failed_batch_is_reported_and_remaining_batches_still_run` (a 403 in the
middle batch: all batches are still attempted, exit code `1`) and
`test_apply_batches_requests_and_sends_favorite_true` (1200 candidates -> `500/500/200`).

The `conditional_albums` suite covers the album side, e.g.
`test_ambiguous_album_name_aborts_before_any_write` (a duplicate album name stops the run before
anything is written) and `test_duplicate_is_reported_but_not_a_failure` (an asset the search
cannot see is reported as `duplicate` instead of failing the run).

The `external_library_scan` suite pins the queue behaviour, e.g.
`test_dry_run_lists_every_library_and_posts_nothing` (a dry run issues zero scan requests) and
`test_one_failing_scan_request_does_not_stop_the_others` (a 400 on the first library still scans the
second, exit code `1`). It also covers a bad `--library` value aborting before any request, an
ambiguous name, a paused queue, a timeout, `--json` output purity (stdout parses as one JSON
document) and the fact that waiting uses a fake clock, so the timeout case is instant and
reproducible. On the reporting side it covers the statistics degradation
(`test_a_failing_statistics_call_degrades_to_unknown_counts`), the `refreshed=never` case and
`test_statistics_are_read_before_the_scans_start`.

## License

MIT, see [LICENSE](LICENSE).
