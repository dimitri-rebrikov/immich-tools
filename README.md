# immich-tools

Small scripts for the [Immich](https://immich.app) API built around its v3 search API.

## Shared code (`immich_api.py`)

All scripts take their HTTP plumbing from `immich_api.py`: the API client with retry/backoff,
`normalize_url`, the `Logger`, `ImmichError` and the argparse type helpers. It is a module, not a
script - the PEP 723 header and the CLI stay in the scripts, so `uv run <script>.py` is enough.
Each script narrows the permissions named in an HTTP 403 message to the ones it actually needs.
`external_library_snapshot.py`, `external_library_smb_snapshot.py` and
`external_library_snapshot_compare.py` are the exception: they never talk to Immich and only use the
`Logger`, so they run without any Immich credentials.

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

## The snapshot trio

Three scripts with one job each: two *producers* list a tree and write a state document, one
*comparer* compares two of those documents. The comparer never touches the disk, the producers never
compare, and nothing else is shared between them.

| Script | Job |
| --- | --- |
| `external_library_snapshot.py` | list a local directory tree |
| `external_library_smb_snapshot.py` | list an SMB share without a mount |
| `external_library_snapshot_compare.py` | compare a persisted baseline with a current state |

A *state* is the document both producers write and the comparer reads:

```json
{"tool": "external_library_snapshot", "version": 1, "generated_at": "2026-09-30T12:00:00+0200",
 "source": "dir:/srv/photos",
 "paths": [{"path": "/srv/photos", "files": {"2026/09/img.jpg": [4821337, 1758051234123456789]},
            "dirs": ["2026", "2026/09"]}]}
```

Any producer that emits that shape works: the comparer only needs `paths[].path/files/dirs`.

## `external_library_snapshot_compare.py`

Compares two states of the same trees - the persisted baseline (`--state`) and the state produced for
this run (`--current`, a file or `-` for stdin) - and prints `changed=yes|no`. It writes nothing. That
is the *condition* half of a dagu DAG: the DAG runs `external_library_scan.py` only when the verdict is
`changed=yes`.

A producer writes both states, so there is one operation per source and one comparison:

```yaml
# immich-library-scan.yaml - produce -> compare -> scan -> produce into the state file
working_dir: /opt/immich-tools
env:
  - IMMICH_URL: https://immich.example.com
  - IMMICH_API_KEY: ${IMMICH_API_KEY}
steps:
  - id: detect
    run: |
      uv run external_library_snapshot.py --path /srv/photos -q | \
        uv run external_library_snapshot_compare.py --state /var/lib/dagu/photos.state.json \
          --current - -q
    output: CHANGED
  - id: scan
    depends: detect
    preconditions:
      - condition: ${CHANGED}
        expected: changed=yes
    run: uv run external_library_scan.py --apply --wait
  - id: record
    depends: scan
    run: >-
      uv run external_library_snapshot.py --path /srv/photos
      --out /var/lib/dagu/photos.state.json
```

Why the DAG looks like this:

- **The verdict is a line, not an exit code.** The comparer exits `0` for `changed=yes` *and* for
  `changed=no`, because dagu treats a step that exits non-zero as failed. The gate is the trailing
  `changed=yes`/`changed=no` line, matched by `output: CHANGED` plus the value-match precondition
  (line-exact, `re:`/`num:` also work). In a shell: `... -q | grep -q '^changed=yes'`.
- `depends: detect` is required: a `${...}` reference inside a precondition does **not** create a
  dependency in dagu, and `${CHANGED}` only resolves once the step has published its output.
- **`record` runs after the scan and only when it succeeded** - dagu does not start a dependent step
  when its dependency was skipped or failed. The baseline therefore never advances on a failed scan
  (Immich down, bad API key, queue paused), and the next run still sees the change.
- **`record` is just the producer again, writing the state file.** It walks a second time, but only
  after something really changed, and the baseline is then the state *after* the scan: no change is
  reported twice, at the price that a file which arrived while the scan was running counts as seen.
  Whatever runs between the producer steps should be idempotent, or keep its own marker.
- First run: there is no baseline yet, so the comparer reports `changed=yes` (reason `no-state`) - the
  DAG scans once and the record step creates it.
- Keep the state file on persistent storage and **outside** the listed trees, or exclude it by name in
  the producer (`--exclude photos.state.json`) - otherwise its own size/mtime changes show up as
  library changes. Other `importPaths` of a library get their own `--path` in the producer steps
  (repeatable); the comparer compares every entry of the documents.

A state that cannot be used fails open: a missing baseline reports `changed=yes` (reason `no-state`),
and a broken or foreign one warns and also reports `changed=yes` (`baseline-unreadable`,
`baseline-malformed`) - one scan too many beats a missed change. A baseline with a different set of
paths reports `paths-changed`.

### Usage

```bash
# produce, compare, act, then let the producer write the baseline
uv run external_library_snapshot.py --path /srv/photos --out photos.now.json
uv run external_library_snapshot_compare.py --state photos.state.json --current photos.now.json --list
uv run external_library_snapshot.py --path /srv/photos --out photos.state.json   # after the action

# shell gate instead of a dagu precondition
uv run external_library_snapshot.py --path /srv/photos -q | \
    uv run external_library_snapshot_compare.py --state photos.state.json --current - -q
```

```console
$ uv run external_library_snapshot_compare.py --state photos.state.json --current photos.now.json --list
baseline: photos.state.json (external_library_snapshot, 2026-09-28T19:31:02+0200, 1 path)
current: /srv/photos (54027 files, 3812 dirs)
changes: added=1 removed=1 modified=1 dirs_added=1 dirs_removed=0
  ~ /srv/photos/2026/urlaub/img_0001.jpg (4821337 -> 4820999 bytes)
  + /srv/photos/2026/urlaub/img_0002.jpg
  - /srv/photos/2026/alt/scan_01.tif
  + dir /srv/photos/2026/neu
changed=yes
```

### What is compared

- Per file: **size and mtime in nanoseconds**. No file content is read, no hash is computed, so a
  state over a 100k-file library is just directory metadata.
- Directories are tracked by name, which is what makes added and removed **empty** directories
  visible. Renames count as `added` + `removed`.
- `--list` names the reason per file: `(1 -> 4 bytes)` for a resized file, `(same size, newer mtime)`
  for one that was rewritten in place. That second case is why the size alone is not enough, and why
  a backup restored with old timestamps is still caught.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--state FILE` | - | the baseline, i.e. the state of the last successful run; **required** |
| `--current FILE` | - | the state produced for this run; `-` reads stdin; **required** |
| `--list` | off | list the changed paths, at most 20 per kind (also in the JSON `details`) |
| `--json` | off | one JSON report on stdout, human lines move to stderr |
| `-q`, `-v` | - | only the verdict / log what is compared |

Exit codes: `0` the verdict was determined (changed or not), `2` configuration error (missing
arguments, an unusable `--current`). A baseline that is missing or unusable is **not** an error: the
comparer reports `changed=yes` and says why - one scan too many beats a missed change.

The JSON report carries `state`, `current`, `producer` (the metadata of the state that was compared),
`changed`, `reason` (`changed`, `unchanged`, `no-state`, `baseline-unreadable`, `baseline-malformed`,
`paths-changed`), `paths` (per path: file/dir counts and its own `changes`) and `changes` (the summed
`added`/`removed`/`modified`/`dirs_added`/`dirs_removed`/`total`); with `--list` it also carries
`details` with the capped path lists plus an `omitted` counter.

## `external_library_snapshot.py`

Lists local directory trees into a state document - the local counterpart of
`external_library_smb_snapshot.py`. `--path` is repeatable and is also the key the comparer stores, so
it has to stay stable between runs.

```bash
# one tree to a file, two trees into the comparer
uv run external_library_snapshot.py --path /srv/photos --out photos.now.json
uv run external_library_snapshot.py --path /srv/photos --path /srv/videos -q | \
    uv run external_library_snapshot_compare.py --state both.state.json --current - -q
```

- `os.scandir`/`DirEntry.stat` only: no file is opened, no content read, no hash computed.
- Symlinks are leaves (never followed), directories are tracked by name (empty ones stay visible),
  `--exclude` matches the path below the root and the bare name (repeatable, or several patterns
  separated by commas), and the noise list (`@eaDir`, `.DS_Store`, `Thumbs.db`, `*.tmp`) is the same
  as the SMB producer's.
- The snapshot goes to stdout, progress to stderr (`-q` silences it), `--list` previews the first
  entries. The file this run writes is never part of its own snapshot.
- Exit codes: `0` snapshot written, `1` a tree could not be read (nothing is written, so a half empty
  tree can never be compared), `2` configuration error.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--path DIR` | - | directory to list, repeatable, **required**; the value is the key in the state |
| `--exclude PATTERN` | - | extra ignore pattern; repeatable, and one flag may carry several separated by commas (defaults always apply) |
| `--out FILE` | `-` | write the snapshot here; `-` is stdout |
| `--list` | off | preview the first entries on stderr |
| `-q`, `-v` | - | quiet progress / log every listed directory |


## `external_library_smb_snapshot.py`

Walks an SMB share **over SMB2/3 without a mount** - no cifs, no FUSE/rclone, no root - and writes the
same state document `external_library_snapshot_compare.py` compares, just from the share instead of a
local path. It exists because a mount turns every `stat` into its own round trip: on a slow share that
costs about a millisecond per *file*. An SMB directory listing already carries size and mtime, so the
same walk needs **one request per directory** plus the listing payload.

The walk is `QUERY_DIRECTORY`/`FileDirectoryInformation` per directory - name, size, mtime and the
directory flag, which is all the comparison needs - exactly the metadata a mount-using client gets,
just through the cheap client path:

```
CONNECT/DELETE-free: 1 x CREATE + 1..n x QUERY_DIRECTORY + 1 x CLOSE  per directory
payload:            ~110 bytes per entry (name, size, FILETIME)
```

- Needs `smbprotocol` (in the PEP 723 header, so `uv run` fetches it), Python 3.10+ and an **SMB2 or
  newer** server (SMB1 cannot be reached). Credentials: NTLM by default, Kerberos with
  `smbprotocol[kerberos]` plus a ticket. The share user only needs read access.
- `--connections N` opens N sessions (N TCP connections, one worker each) and shards the walk over
  them. That is the lever when a single session is capped by the server, which is the normal case.
  Measured on a NAS, walking 1 038 directories with 54 336 files:

  | Sessions | Wall time | Entries/s |
  | --- | --- | --- |
  | 1 | 79,5 s | 700 |
  | 2 | 29,3 s | 1 890 |
  | 4 | 22,9 s | 2 420 |
  | 8 | 22,1 s | 2 500 |
  | 16 | 23,1 s | 2 400 |
  | 32 | 26,6 s | 2 080 |
  | 16 threads on 1 session | 39,0 s | 1 420 |

  The knee is at 4 to 8 sessions - **8 is the value to use**; a difference between 8 and 16 is noise,
  and 32 was slower (26,6 s). The same tree measured 23-35 s across runs, so the NAS's own load shows
  up as much as any setting. A refused session is a warning and only "no session at all" is fatal.
- Same filtering rules as the local producer: `--exclude` applies to files and directories (an
  excluded directory is not descended into, which is the only way to save real work; repeatable, or
  several patterns separated by commas), the usual NAS noise (`@eaDir`, `.DS_Store`, `Thumbs.db`,
  `*.tmp`) is always ignored, symlinks/reparse points stay leaves, and directories are tracked by name
  so empty ones remain visible.
- Output: `{"tool", "version", "generated_at", "source", "paths": [{"path", "files", "dirs"}],
  "unreadable", "elapsed_seconds"}`, with `files` mapping a path relative to the start to
  `[size, mtime_ns]`.
- The snapshot goes to **stdout**, progress and warnings to **stderr** (`-q` silences the progress), so
  producer and comparer compose in one pipeline.
- Exit codes: `0` snapshot written, `1` the share or the start directory could not be read (nothing is
  written, so a half empty tree can never be compared), `2` configuration error (missing credentials,
  unwritable `--out`).

### Usage

```bash
# write a state next to the baseline
SMB_USER=immich SMB_PASSWORD=... uv run external_library_smb_snapshot.py \
    --host nas --share photos --key /srv/photos --out /var/lib/dagu/photos.now.json

# or straight into the comparer, without a file in between
SMB_USER=immich SMB_PASSWORD=... uv run external_library_smb_snapshot.py \
    --host nas --share photos --key /srv/photos -q \
    | uv run external_library_snapshot_compare.py --state /var/lib/dagu/photos.state.json --current - -q

# a subtree of a share, walking with 8 sessions, password from a 600 file
SMB_USER=immich uv run external_library_smb_snapshot.py --host nas --share photos --subdir 2026 \
    --key /srv/photos --password-file /etc/immich-smb.cred --connections 8 --out photos.now.json
```

### In a DAG

The SMB producer replaces the local one; everything else is the same DAG:

```yaml
steps:
  - id: detect
    run: |
      uv run external_library_smb_snapshot.py --host nas --share photos --key /srv/photos \
        --password-file /etc/immich-smb.cred --connections 8 -q | \
        uv run external_library_snapshot_compare.py --state /var/lib/dagu/photos.state.json \
          --current - -q
    output: CHANGED
  - id: scan
    depends: detect
    preconditions:
      - condition: ${CHANGED}
        expected: changed=yes
    run: uv run external_library_scan.py --apply --wait
  - id: record
    depends: scan
    run: >-
      uv run external_library_smb_snapshot.py --host nas --share photos --key /srv/photos
      --password-file /etc/immich-smb.cred --connections 8 --out /var/lib/dagu/photos.state.json
```

`--key` is the name this tree is remembered under - the `path` in the state. Leave it out while the SMB
producer is the library's only source; pass the path a mount-based producer would use
(`--key /srv/photos`) when such a state already exists, so the switch costs one `changed=yes`
(`paths-changed`) and both producers can share one state. The record step walks the share a second
time (~20 s on the example NAS) - it only runs when something really changed.

### Limits worth knowing

- A single session is capped by the server, not by the client: SMB2 allows only as many requests in
  flight as the server granted credits for, and servers also cap their own request handling. Each
  session asks for the same fixed window (`SESSION_CREDITS`, independent of `--connections`, because
  one session now feeds one worker) and `-v` prints the answer (`credits: 16 (requested 16)`); the
  same share ran 2x faster with 16 threads on one session and 3.4x faster with 16 sessions.
- What remains is the server's cost per directory **entry**: on that share a listing of 1 143 files
  took 0,79 s with size+mtime and 0,67 s with names only, so ~0,6 ms are charged per entry no matter
  what is asked for - 55 374 entries therefore cost ~24 s whatever the client does. Asking for
  `FileIdBothDirectoryInformation` instead of `FileDirectoryInformation`, or for names only, does not
  change it. Only asking for fewer entries helps - which costs resolution.
- It is the same server: if the **server** is slow per entry (a NAS filling attributes for every
  entry of the listing) or the link is saturated, this walk is slow too. Only a client that avoids the
  enumeration can help there, not a faster client.
- Changing the source of an existing library (mount walk <-> SMB snapshot) reports every file as
  `modified` once and is quiet afterwards: both describe the same server timestamp, but the SMB path
  rounds to microseconds.
- It is a one-shot snapshot, not a watcher: it cannot tell you "something happened 30 seconds ago".
- Two SMB clients on one host (rclone mount + this walk) are fine; they are separate sessions.
## Tests

```bash
uv run tests/test_favorite_rated.py
uv run tests/test_conditional_albums.py
uv run tests/test_external_library_scan.py
uv run tests/test_external_library_snapshot.py
uv run tests/test_external_library_snapshot_compare.py
uv run tests/test_external_library_smb_snapshot.py
```

All suites fake the HTTP layer, so they never touch a real server. The failure paths above are
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

The `external_library_snapshot` suite runs the local producer against temp directories: deep nesting,
empty directories, symlinks as leaves, the exclude rules, an unreadable subdirectory (counted, not
fatal) and an unreadable root (`test_an_unreadable_root_fails_without_writing_a_snapshot`, skipped on
Windows where `chmod` does not make a directory unreadable). Its last test feeds a produced document
through the comparer and checks that not a single entry changes on the way.

The `external_library_snapshot_compare` suite works on synthetic states, because the comparer never
touches the filesystem: the diff itself (added/removed/modified, plus `(1 -> 4 bytes)` and
`(same size, newer mtime)` in `--list`), the fail-open cases (a missing, broken or foreign baseline
reports `changed=yes`), `paths-changed`, the stdin variant, `--json` and every configuration error (an
unusable `--current` is exit `2`). `test_nothing_is_written_anywhere` pins that the comparer leaves both
documents alone, and two tests drive the real producer through it (`produce -> compare -> produce ->
compare`, plus the pipeline form).

The `external_library_smb_snapshot` suite needs neither a server nor the `smbprotocol` package: the
walk takes an injected directory lister, so the SMB specifics are the only untested part. It pins the
walk itself (`test_parallel_connections_produce_the_same_result`,
`test_unreadable_subdirectory_is_counted_but_not_fatal`, an excluded directory that must never be
listed), the FILETIME conversion, the reparse-point handling, the version-tolerant session arguments
(`test_session_credentials_fit_the_installed_smbprotocol_version`), the credit window (the session id
is used, a refused credit request stays non-fatal) and the sessions (one per connection, all closed
again, a refused session degrades to a warning, no session at all is exit `1`, and a session is never
handed to two listings at once). `test_the_produced_document_becomes_the_baseline` runs the produced
document through the comparer for real. `tests/test_external_library_snapshot.py` keeps both noise
lists identical.

## License

MIT, see [LICENSE](LICENSE).
