# immich-tools

Small scripts for the [Immich](https://immich.app) API built around its v3 search API.

## Shared code (`immich_api.py`)

Both scripts take their HTTP plumbing from `immich_api.py`: the API client with retry/backoff,
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

## Tests

```bash
uv run tests/test_favorite_rated.py
uv run tests/test_conditional_albums.py
```

Both suites fake the HTTP layer, so they never touch a real server. The failure paths above are
covered too, e.g. `test_failed_batch_is_reported_and_remaining_batches_still_run` (a 403 in the
middle batch: all batches are still attempted, exit code `1`) and
`test_apply_batches_requests_and_sends_favorite_true` (1200 candidates -> `500/500/200`).

The `conditional_albums` suite covers the album side, e.g.
`test_ambiguous_album_name_aborts_before_any_write` (a duplicate album name stops the run before
anything is written) and `test_duplicate_is_reported_but_not_a_failure` (an asset the search
cannot see is reported as `duplicate` instead of failing the run).

## License

MIT, see [LICENSE](LICENSE).
