# immich-tools

Small scripts for the [Immich](https://immich.app) API.

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
| `-q`, `-v` | - | quiet progress / log every HTTP request |

Exit codes: `0` success (or nothing to do), `1` at least one update batch failed,
`2` configuration or API error before any write.

### Tests

```bash
uv run tests/test_favorite_rated.py
```

The HTTP layer is faked, so the suite never touches a real server. The failure paths above are
covered too, e.g. `test_failed_batch_is_reported_and_remaining_batches_still_run` (a 403 in the
middle batch: all batches are still attempted, exit code `1`) and
`test_apply_batches_requests_and_sends_favorite_true` (1200 candidates -> `500/500/200`).

## License

MIT, see [LICENSE](LICENSE).
